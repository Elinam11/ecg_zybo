# Headless Vivado build: project -> block design -> synthesis -> implementation ->
# bitstream -> XSA.
#
# Run from an OS / Vivado Command Prompt shell (NOT from a Vivado% Tcl console):
#
#   vivado -mode batch -source vivado/build_vivado.tcl
#   vivado -mode batch -source vivado/build_vivado.tcl -tclargs -record 208
#   vivado -mode batch -source vivado/build_vivado.tcl -tclargs -bd_only
#   vivado -mode batch -source vivado/build_vivado.tcl -tclargs -board digilentinc.com:zybo-z7-10:part0:1.1
#
# Options:
#   -record <id>   record whose .coe initialises the sample store (default 100)
#   -board <part>  exact BOARD_PART string (default: auto-detect Zybo Z7-10)
#   -bd_only       stop after creating and validating the block design
#   -jobs <n>      parallel synthesis/implementation jobs
#
# Digilent board files must be visible to this Vivado version (copied under
# data/boards/board_files, or loaded via BOARD_PART_REPO_PATHS / Vivado_init.tcl).

set ECG_VIVADO_DIR [file normalize [file dirname [info script]]]
set ECG_REPO       [file normalize "$ECG_VIVADO_DIR/.."]

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
set opt_record  "100"
# Prefer auto-detect over a pinned revision: Digilent board files ship as
# digilentinc.com:zybo-z7-10:part0:1.0, :1.1, … depending on the vivado-boards
# checkout and Vivado version. A hard-coded :1.0 fails when only a newer
# revision is installed, even though the GUI still lists "Zybo Z7-10".
set opt_board   ""
set opt_part    "xc7z010clg400-1"
set opt_bd_only 0
set opt_jobs    4

for {set i 0} {$i < [llength $argv]} {incr i} {
    switch -- [lindex $argv $i] {
        "-record"  { incr i; set opt_record [lindex $argv $i] }
        "-board"   { incr i; set opt_board  [lindex $argv $i] }
        "-part"    { incr i; set opt_part   [lindex $argv $i] }
        "-jobs"    { incr i; set opt_jobs   [lindex $argv $i] }
        "-bd_only" { set opt_bd_only 1 }
        default    { puts "WARNING: ignoring unknown argument [lindex $argv $i]" }
    }
}

# Resolve the Digilent Zybo Z7-10 board part installed on this machine.
proc ecg_resolve_board_part {preferred} {
    if {$preferred ne "" && [llength [get_board_parts -quiet $preferred]] > 0} {
        return $preferred
    }
    set matches [get_board_parts -quiet -filter {BOARD_NAME =~ "*zybo*z7*10*" || NAME =~ "*zybo-z7-10*"}]
    if {[llength $matches] == 0} {
        set matches [get_board_parts -quiet *zybo-z7-10*]
    }
    if {[llength $matches] == 0} {
        set matches [get_board_parts -quiet *zybo*]
    }
    if {[llength $matches] == 0} {
        return ""
    }
    # Newest revision last in typical NAME sort (…:part0:1.0, …:1.1, …).
    return [lindex [lsort $matches] end]
}

set proj_dir  "$ECG_VIVADO_DIR/proj_ecg"
set proj_name "ecg_zybo"
set ip_repo   "$ECG_REPO/hls/proj_top/solution1/impl/ip"
set ::ECG_COE_FILE "$ECG_REPO/data/coe/${opt_record}_x32.coe"

puts "==============================================================="
puts " ECG compression: Vivado build"
puts "   part   : $opt_part"
puts "   board  : [expr {$opt_board eq "" ? "(auto-detect Zybo Z7-10)" : $opt_board}]"
puts "   record : $opt_record"
puts "   coe    : $::ECG_COE_FILE"
puts "==============================================================="
puts "NOTE: run this from an OS shell, not from a Vivado% Tcl prompt:"
puts "  vivado -mode batch -source vivado/build_vivado.tcl"
puts "==============================================================="

if {![file exists $::ECG_COE_FILE]} {
    error "Missing $::ECG_COE_FILE. Run: python python/scripts/gen_coe.py $opt_record"
}
if {![file exists "$ip_repo/component.xml"]} {
    error "Exported HLS IP not found at $ip_repo.\
           Run: vitis_hls -f hls/tcl/top_integrate.tcl"
}

# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------
file delete -force $proj_dir
create_project $proj_name $proj_dir -part $opt_part -force

set resolved_board [ecg_resolve_board_part $opt_board]
if {$resolved_board eq ""} {
    set available [get_board_parts -quiet *]
    error "No Zybo Z7-10 board part is visible to this Vivado session.\n\
           The GUI may still list the board if Digilent files were added via\n\
           BOARD_PART_REPO_PATHS / Vivado_init.tcl — confirm that path is active\n\
           here too. In the Vivado Tcl console run:\n\
             get_board_parts *zybo*\n\
           then re-run with:\n\
             vivado -mode batch -source vivado/build_vivado.tcl -tclargs -board <exact-name>\n\
           Available board_parts count: [llength $available]."
}
set opt_board $resolved_board
puts "Using BOARD_PART: $opt_board"
set_property BOARD_PART $opt_board [current_project]

set_property ip_repo_paths $ip_repo [current_project]
update_ip_catalog -rebuild

# ---------------------------------------------------------------------------
# Block design
# ---------------------------------------------------------------------------
source "$ECG_VIVADO_DIR/create_bd.tcl"

set bd_file [get_files "${::BD_NAME}.bd"]
make_wrapper -files $bd_file -top
add_files -norecurse "$proj_dir/${proj_name}.gen/sources_1/bd/${::BD_NAME}/hdl/${::BD_NAME}_wrapper.v"
set_property top "${::BD_NAME}_wrapper" [current_fileset]

add_files -fileset constrs_1 -norecurse "$ECG_VIVADO_DIR/constraints/ecg_zybo.xdc"

# Re-emit the block design as a script. This is the canonical, reviewable form of the
# BD: if it is ever edited in the GUI, committing the regenerated file keeps the
# headless build and the GUI in step.
write_bd_tcl -force "$ECG_VIVADO_DIR/gen_bd_from_gui.tcl"
puts "wrote $ECG_VIVADO_DIR/gen_bd_from_gui.tcl"

if {$opt_bd_only} {
    puts "-bd_only: stopping after block design creation."
    exit 0
}

# ---------------------------------------------------------------------------
# Synthesis and implementation (area-first: XC7Z010 is LUT/slice bound)
# ---------------------------------------------------------------------------
# Flow_AreaOptimized_high + Area_Explore target LUT/slice packing. Control-set
# count on this design is high (HLS pipelines); ExploreArea merges equivalent
# drivers / CE where safe so FFs can share slices (Place 30-487).
set_property strategy Flow_AreaOptimized_high [get_runs synth_1]
set_property strategy Area_Explore [get_runs impl_1]
set_property STEPS.OPT_DESIGN.ARGS.DIRECTIVE ExploreArea [get_runs impl_1]
# Default place (not ExtraNetDelay): packing matters more than wire delay here.
set_property STEPS.PLACE_DESIGN.ARGS.DIRECTIVE Default [get_runs impl_1]

launch_runs synth_1 -jobs $opt_jobs
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    error "Synthesis failed. See $proj_dir/${proj_name}.runs/synth_1"
}

open_run synth_1
# #region agent log
set _util_pre [report_utilization -return_string]
set _lut_line ""
set _ff_line ""
set _bram_line ""
set _slice_line ""
foreach _ln [split $_util_pre "\n"] {
    if {[string match "*Slice LUTs*" $_ln] && ![string match "*Logic*" $_ln]} {
        set _lut_line [string trim $_ln]
    }
    if {[string match "*Slice Registers*" $_ln]} { set _ff_line [string trim $_ln] }
    if {[string match "*Block RAM Tile*" $_ln]} { set _bram_line [string trim $_ln] }
    if {[string match "*Slices*" $_ln] && ![string match "*LUT*" $_ln]} {
        set _slice_line [string trim $_ln]
    }
}
proc _ecg_json_escape {s} {
    return [string map {\\ \\\\ \" \\\" \n \\n \r \\r} $s]
}
set _lfh [open [file join $ECG_REPO "debug-0362fe.log"] a]
puts $_lfh "{\"sessionId\":\"0362fe\",\"runId\":\"pre-fix\",\"hypothesisId\":\"H2\",\"location\":\"build_vivado.tcl:post-synth\",\"message\":\"post-synth utilization\",\"data\":{\"lut_line\":\"[_ecg_json_escape $_lut_line]\",\"ff_line\":\"[_ecg_json_escape $_ff_line]\",\"bram_line\":\"[_ecg_json_escape $_bram_line]\",\"slice_line\":\"[_ecg_json_escape $_slice_line]\",\"device_lut\":17600,\"device_slice\":4400,\"device_bram36\":60},\"timestamp\":[clock milliseconds]}"
close $_lfh
# #endregion

launch_runs impl_1 -to_step write_bitstream -jobs $opt_jobs
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] != "100%"} {
    # #region agent log
    set _impl_dir "$proj_dir/${proj_name}.runs/impl_1"
    set _place_msg ""
    set _ctrl_msg ""
    foreach _cand [list \
        [file join $_impl_dir "runme.log"] \
        [file join $_impl_dir "place_design.log"] \
        [file join $_impl_dir "${::BD_NAME}_wrapper.vdi"]] {
        if {![file exists $_cand]} { continue }
        set _fh [open $_cand r]
        set _txt [read $_fh]
        close $_fh
        foreach _ln [split $_txt "\n"] {
            if {[string match "*Place 30-487*" $_ln]} { set _place_msg [string trim $_ln] }
            if {[string match "*Control sets:*" $_ln]} { set _ctrl_msg [string trim $_ln] }
            if {[string match "*Luts:*" $_ln]} { append _ctrl_msg " | [string trim $_ln]" }
        }
    }
    set _lfh [open [file join $ECG_REPO "debug-0362fe.log"] a]
    puts $_lfh "{\"sessionId\":\"0362fe\",\"runId\":\"pre-fix\",\"hypothesisId\":\"H4\",\"location\":\"build_vivado.tcl:impl-fail\",\"message\":\"implementation failed\",\"data\":{\"place_msg\":\"[_ecg_json_escape $_place_msg]\",\"ctrl_msg\":\"[_ecg_json_escape $_ctrl_msg]\"},\"timestamp\":[clock milliseconds]}"
    close $_lfh
    # #endregion
    error "Implementation failed. See $proj_dir/${proj_name}.runs/impl_1"
}

open_run impl_1

# ---------------------------------------------------------------------------
# Report: timing and utilisation against the Phase 3 budget
# ---------------------------------------------------------------------------
set report_dir "$ECG_REPO/data/reports"
file mkdir $report_dir
report_utilization -file "$report_dir/utilization_${opt_record}.rpt"
report_timing_summary -file "$report_dir/timing_${opt_record}.rpt"

set wns [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]]
set whs [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -hold]]
puts "---------------------------------------------------------------"
puts " setup WNS : $wns ns"
puts " hold  WHS : $whs ns"
set util [report_utilization -return_string]
set _lut_ok ""
set _bram_ok ""
foreach line [split $util "\n"] {
    if {[regexp {\|\s*(Slice LUTs|Slice Registers|Block RAM Tile|DSPs)\s*\|} $line]} {
        puts " $line"
    }
    if {[string match "*Slice LUTs*" $line] && ![string match "*Logic*" $line]} {
        set _lut_ok [string trim $line]
    }
    if {[string match "*Block RAM Tile*" $line]} { set _bram_ok [string trim $line] }
}
puts "---------------------------------------------------------------"
# #region agent log
set _lfh [open [file join $ECG_REPO "debug-0362fe.log"] a]
puts $_lfh "{\"sessionId\":\"0362fe\",\"runId\":\"post-fix\",\"hypothesisId\":\"H2\",\"location\":\"build_vivado.tcl:post-impl\",\"message\":\"implementation succeeded\",\"data\":{\"wns\":\"$wns\",\"whs\":\"$whs\",\"lut_line\":\"[_ecg_json_escape $_lut_ok]\",\"bram_line\":\"[_ecg_json_escape $_bram_ok]\"},\"timestamp\":[clock milliseconds]}"
close $_lfh
# #endregion

if {$wns < 0} {
    error "Timing not met: setup WNS $wns ns. The design must close timing at\
           100 MHz before hardware bring-up."
}

# ---------------------------------------------------------------------------
# Hardware handoff
# ---------------------------------------------------------------------------
set xsa "$ECG_VIVADO_DIR/ecg_zybo.xsa"
write_hw_platform -fixed -include_bit -force -file $xsa
puts "wrote $xsa"
puts "wrote $proj_dir/${proj_name}.runs/impl_1/${::BD_NAME}_wrapper.bit"
puts "Build complete."
exit 0
