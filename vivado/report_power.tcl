# Open the closed impl and write a power report (no rebuild).
set repo [file normalize [file dirname [info script]]/..]
set dcp  "$repo/vivado/proj_ecg/ecg_zybo.runs/impl_1/ecg_system_wrapper_routed.dcp"
set outdir "$repo/data/reports"
file mkdir $outdir

if {![file isfile $dcp]} {
    error "Routed checkpoint not found: $dcp"
}

open_checkpoint $dcp
report_power -file "$outdir/power_100.rpt"
puts "wrote $outdir/power_100.rpt"
