# Close leftover WNS on the last routed checkpoint without re-placing.
# ECG core already meets 100 MHz; the last two violators are SmartConnect ->
# PS HP0 AWSIZE nets (chip-crossing route at 100% slice occupancy).
set ECG_VIVADO_DIR [file normalize [file dirname [info script]]]
set ECG_REPO       [file normalize "$ECG_VIVADO_DIR/.."]
set proj_dir       "$ECG_VIVADO_DIR/proj_ecg"
set routed         "$proj_dir/ecg_zybo.runs/impl_1/ecg_system_wrapper_routed.dcp"

open_checkpoint $routed

proc ecg_wns {} {
    return [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]]
}

puts "starting WNS [ecg_wns] ns"

# Unroute the actual violating nets so TNS-cleanup can pick a shorter path.
set vpaths [get_timing_paths -max_paths 16 -nworst 1 -setup -slack_lesser_than 0]
set vnets [list]
foreach p $vpaths {
    foreach n [get_nets -quiet -of_objects $p] {
        if {$n ne ""} { lappend vnets $n }
    }
}
set vnets [lsort -unique $vnets]
if {[llength $vnets] > 0} {
    puts "unrouting [llength $vnets] violating nets"
    catch {route_design -unroute -nets $vnets}
}

route_design -directive Explore -tns_cleanup
puts "after tns_cleanup WNS [ecg_wns] ns"

foreach dir {AggressiveExplore AggressiveFanoutOpt AlternateFlowWithRetiming Explore} {
    if {[ecg_wns] >= 0} { break }
    puts "phys_opt_design -directive $dir"
    phys_opt_design -directive $dir
    puts "WNS now [ecg_wns] ns"
    if {[ecg_wns] < 0} {
        route_design -directive Explore -tns_cleanup
        puts "after reroute WNS [ecg_wns] ns"
    }
}

set wns [ecg_wns]
set report_dir "$ECG_REPO/data/reports"
file mkdir $report_dir
report_timing_summary -file "$report_dir/timing_100.rpt"
report_utilization -file "$report_dir/utilization_100.rpt"
puts "setup WNS : $wns ns"

if {$wns < 0} {
    error "Timing not met after post-route phys_opt: WNS $wns ns"
}

write_checkpoint -force "$proj_dir/ecg_zybo.runs/impl_1/ecg_system_wrapper_routed.dcp"
write_bitstream -force "$proj_dir/ecg_zybo.runs/impl_1/ecg_system_wrapper.bit"

open_project "$proj_dir/ecg_zybo.xpr"
open_run impl_1
write_hw_platform -fixed -include_bit -force -file "$ECG_VIVADO_DIR/ecg_zybo.xsa"
puts "wrote $ECG_VIVADO_DIR/ecg_zybo.xsa"
puts "Build complete."
exit 0
