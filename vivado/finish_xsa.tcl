# Write XSA from the closed routed checkpoint (impl_1 is marked Failed so
# open_run cannot be used). Bitstream already written by close_wns.tcl.
set ECG_VIVADO_DIR [file normalize [file dirname [info script]]]
set proj_dir       "$ECG_VIVADO_DIR/proj_ecg"
set bit            "$proj_dir/ecg_zybo.runs/impl_1/ecg_system_wrapper.bit"
set dcp            "$proj_dir/ecg_zybo.runs/impl_1/ecg_system_wrapper_routed.dcp"
set xsa            "$ECG_VIVADO_DIR/ecg_zybo.xsa"

open_project "$proj_dir/ecg_zybo.xpr"
open_checkpoint $dcp
set wns [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]]
puts "open WNS $wns ns"
if {$wns < 0} {
    error "Checkpoint WNS $wns ns; expected the closed +0.021 ns route"
}
if {![file exists $bit]} {
    error "Missing bitstream $bit"
}
write_hw_platform -fixed -include_bit -force -file $xsa
puts "wrote $xsa"
puts "Build complete."
exit 0
