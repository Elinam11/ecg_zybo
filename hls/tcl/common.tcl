# Shared setup for every Vitis HLS project in this design.
#
# Sourced by the per-module scripts; never run directly. Keeps the part, clock, include
# paths and golden-vector location defined exactly once.

# ---------------------------------------------------------------------------
# Paths. This file lives in <repo>/hls/tcl, so the repo root is two levels up.
# ---------------------------------------------------------------------------
set ECG_TCL_DIR  [file normalize [file dirname [info script]]]
set ECG_HLS_DIR  [file normalize "$ECG_TCL_DIR/.."]
set ECG_REPO     [file normalize "$ECG_HLS_DIR/.."]
set ECG_SRC      "$ECG_HLS_DIR/src"
set ECG_TB       "$ECG_HLS_DIR/tb"
set ECG_DATA     "$ECG_REPO/data"

# The testbenches read these at runtime, so csim finds the golden vectors no matter
# which build subdirectory Vitis HLS runs them from.
set ::env(ECG_GOLDEN_DIR) "$ECG_DATA/golden"
set ::env(ECG_COE_DIR)    "$ECG_DATA/coe"

# ---------------------------------------------------------------------------
# Target: Zybo Z7-10, Zynq-7010, speed grade -1
# ---------------------------------------------------------------------------
set ECG_PART        "xc7z010clg400-1"
set ECG_CLK_PERIOD  10.0      ;# 100 MHz PL clock
set ECG_CLK_UNCERT  1.25

# Which record the testbenches run against. Override with:
#   vitis_hls -f hls/tcl/run_all.tcl -tclargs 208
#
# IMPORTANT: when launched as `vitis_hls -f <script>`, Tcl's ::argv still contains
# Vitis HLS's own CLI tokens (notably "-f" and the script path). Blindly taking
# argv[0] therefore turns the tool flag into the record id and csim looks for
# data/golden/-f/. Only accept a non-flag, non-.tcl token (the -tclargs value).
set ECG_RECORD "100"
if {[info exists ::argv]} {
    foreach arg $::argv {
        if {[string match "-*" $arg]} {
            continue
        }
        if {[string match "*.tcl" $arg]} {
            continue
        }
        if {[regexp {^[0-9A-Za-z_]+$} $arg]} {
            set ECG_RECORD $arg
            break
        }
    }
}

set ECG_CFLAGS "-I$ECG_SRC -I$ECG_TB -std=c++14"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Create a project/solution for one module and add its sources.
#   name    project directory name
#   top     top-level function to synthesise
#   sources list of .cpp files under hls/src
#   tb      testbench file under hls/tb (may be empty)
proc ecg_open_module {name top sources tb} {
    global ECG_SRC ECG_TB ECG_PART ECG_CLK_PERIOD ECG_CLK_UNCERT ECG_CFLAGS
    global ECG_HLS_DIR

    open_project -reset "$ECG_HLS_DIR/proj_$name"
    set_top $top

    foreach f $sources {
        add_files "$ECG_SRC/$f" -cflags $ECG_CFLAGS
    }
    if {$tb ne ""} {
        add_files -tb "$ECG_TB/$tb" -cflags $ECG_CFLAGS
    }

    open_solution -reset "solution1" -flow_target vivado
    set_part $ECG_PART
    create_clock -period $ECG_CLK_PERIOD -name default
    set_clock_uncertainty $ECG_CLK_UNCERT
    # Prefer control-register reset only: full datapath reset nets explode the
    # control-set count and trigger Place 30-487 on the XC7Z010 (4400 slices).
    config_rtl -reset control
}

# The interpreter used for the Python reference model. Override with ECG_PYTHON when
# `python` is not on PATH (common on Windows, where the Store alias shadows it).
proc ecg_python {} {
    if {[info exists ::env(ECG_PYTHON)]} {
        return $::env(ECG_PYTHON)
    }
    return "python"
}

# RTL co-simulation cannot afford 108,000 samples: it would take hours. Compress a
# 30-second excerpt instead, which still exercises every code path (both coders, the
# packer, TLAST) end to end. Golden vectors for it are generated on demand.
set ECG_COSIM_RECORD  "cosim"
set ECG_COSIM_SAMPLES 10800     ;# 30 s at 360 Hz

proc ecg_ensure_cosim_golden {} {
    global ECG_DATA ECG_REPO ECG_RECORD ECG_COSIM_RECORD ECG_COSIM_SAMPLES
    set dir "$ECG_DATA/golden/$ECG_COSIM_RECORD"
    # Always regenerate: a leftover Haar (or otherwise stale) bitstream_words.txt
    # makes C-TB fail before RTL simulation even starts.
    puts "Generating $ECG_COSIM_SAMPLES-sample cosim golden vectors from record $ECG_RECORD..."
    if {[catch {
        exec [ecg_python] "$ECG_REPO/python/scripts/run_reference.py" $ECG_RECORD \
            --golden --samples $ECG_COSIM_SAMPLES --golden-name $ECG_COSIM_RECORD
    } result]} {
        puts "WARNING: could not generate cosim vectors: $result"
        return 0
    }
    puts $result
    return 1
}

# Report the headline utilisation/timing numbers so a headless run is self-documenting.
proc ecg_report_summary {name} {
    global ECG_HLS_DIR ECG_REPO
    set path "$ECG_HLS_DIR/proj_$name/solution1/syn/report/csynth.rpt"
    if {![file exists $path]} {
        puts "  (no csynth report at $path)"
        return
    }
    puts "---- $name synthesis summary ----"
    set fh [open $path r]
    set text [read $fh]
    close $fh
    set top_lut ""
    set top_bram ""
    set energy_lut ""
    set encode_lut ""
    set bitpack_luts {}
    set peaks_impl ""
    set alloc_ignored 0
    foreach line [split $text "\n"] {
        if {[regexp {\|\s*(Total|Available|Utilization \(%\))\s*\|} $line] ||
            [regexp {Latency|BRAM_18K|DSP|FF|LUT|URAM} $line]} {
            puts "  $line"
        }
        if {[string match "*|+ ecg_top*" $line] || [string match "*| + ecg_top*" $line]} {
            # Utilization table: BRAM DSP FF LUT [URAM] — each "N (P%)" or "-"
            if {[regexp {\|\s+(\d+)\s+\([^)]+\)\s+\|\s+(\d+)\s+\([^)]+\)\s+\|\s+(\d+)\s+\([^)]+\)\s+\|\s+(\d+)\s+\((\d+)%\)} $line \
                    -> bram dsp ff lut pct]} {
                set top_bram $bram
                set top_lut $lut
            } elseif {[regexp {\|\s+(\d+)\s+\((\d+)%\)\s+\|\s*$} $line -> lut pct]} {
                set top_lut $lut
            }
        }
        if {[string match "*| + ecg_encode_stage*" $line] || [string match "*|+ ecg_encode_stage*" $line]} {
            if {![string match "*Pipeline*" $line]} {
                if {[regexp {\|\s+(\d+)\s+\((\d+)%\)\s+\|\s+-?\s*\|?\s*$} $line -> lut pct] ||
                    [regexp {\|\s+(\d+)\s+\((\d+)%\)\s+\|} $line -> lut pct]} {
                    # Prefer the LUT column (last N (P%) before URAM)
                    if {[regexp {\|\s+(\d+)\s+\([^)]*\)\s+\|\s+(\d+)\s+\([^)]*\)\s+\|\s+(\d+)\s+\([^)]*\)\s+\|\s+(\d+)\s+\((\d+)%\)} $line \
                            -> _b _d _f elut epct]} {
                        set encode_lut $elut
                    }
                }
            }
        }
        if {[string match "*ecg_energy_step*" $line]} {
            if {[regexp {\|\s+(\d+)\s+\((\d+)%\)\s+\|\s*$} $line -> lut pct]} {
                set energy_lut $lut
            }
        }
        if {[regexp {\|\s+\+?\s*ecg_bitpack_put\s+\|} $line]} {
            if {[regexp {\|\s+(\d+)\s+\([^)]*\)\s+\|\s+(\d+)\s+\([^)]*\)\s+\|\s+(\d+)\s+\([^)]*\)\s+\|\s+(\d+)\s+\((\d+)%\)} $line \
                    -> _b _d _f blut bpct]} {
                lappend bitpack_luts $blut
            }
        }
        if {[string match "*peaks*" $line] && [string match "*LUTRAM*" $line]} {
            set peaks_impl "lutram"
        }
        if {[string match "*peaks*" $line] &&
            ([string match "*BRAM*" $line] || [string match "*bram*" $line])} {
            set peaks_impl "bram"
        }
        if {[string match "*Ignoring ALLOCATION*" $line]} {
            incr alloc_ignored
        }
    }
    set slog "$ECG_HLS_DIR/proj_$name/solution1/solution1.log"
    if {[file exists $slog]} {
        set sfh [open $slog r]
        set stext [read $sfh]
        close $sfh
        foreach line [split $stext "\n"] {
            if {[string match "*Ignoring ALLOCATION*" $line]} { incr alloc_ignored }
            if {[string match "*peaks*LUTRAM*" $line]} { set peaks_impl "lutram" }
            if {[string match "*peaks*BRAM*" $line] ||
                [string match "*peaks_RAM*BRAM*" $line]} { set peaks_impl "bram" }
        }
    }
    set bitpack_count [llength $bitpack_luts]
    set bitpack_sum 0
    foreach v $bitpack_luts { incr bitpack_sum $v }
    puts "  bitpack_put instances=$bitpack_count lut_sum=$bitpack_sum peaks=$peaks_impl alloc_ignored=$alloc_ignored"
    # #region agent log
    set logpath [file join $ECG_REPO "debug-0362fe.log"]
    set lfh [open $logpath a]
    set ts [clock milliseconds]
    set crash_hint 0
    if {[file exists "$ECG_HLS_DIR/proj_$name/solution1/.autopilot/db/a.g.ld.0.bc.clang.reflow.err.log"]} {
        set efh [open "$ECG_HLS_DIR/proj_$name/solution1/.autopilot/db/a.g.ld.0.bc.clang.reflow.err.log" r]
        set etxt [read $efh]
        close $efh
        if {[string match "*crash dump*" $etxt] || [string match "*failed due to signal*" $etxt]} {
            set crash_hint 1
        }
    }
    set dup_nonqrs 0
    if {[string match "*load_segment1*" $text] || [string match "*count_tokens4*" $text]} {
        set dup_nonqrs 1
    }
    puts $lfh "{\"sessionId\":\"0362fe\",\"runId\":\"pre-fix\",\"hypothesisId\":\"H7\",\"location\":\"common.tcl:ecg_report_summary\",\"message\":\"csynth LUT snapshot\",\"data\":{\"module\":\"$name\",\"ecg_top_lut\":\"$top_lut\",\"ecg_top_bram18\":\"$top_bram\",\"encode_stage_lut\":\"$encode_lut\",\"bitpack_instances\":$bitpack_count,\"bitpack_lut_sum\":$bitpack_sum,\"peaks_impl\":\"$peaks_impl\",\"alloc_ignored\":$alloc_ignored,\"clang_crash\":$crash_hint,\"dup_nonqrs_pipelines\":$dup_nonqrs,\"device_lut\":17600,\"prior_failed_top_lut\":17630,\"prior_failed_combined_lut\":19259,\"target_combined_lut\":17000},\"timestamp\":$ts}"
    close $lfh
    # #endregion
}
