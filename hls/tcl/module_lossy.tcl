# Module 3: lossy Haar DWT + threshold + RLE coder.
#   vitis_hls -f hls/tcl/module_lossy.tcl [-tclargs <record>]
source [file join [file dirname [info script]] common.tcl]

puts "=== Module 3: lossy (record $ECG_RECORD) ==="
ecg_open_module lossy ecg_encode_nonqrs_top {bitpack.cpp lossy.cpp} tb_lossy.cpp

csim_design -argv "$ECG_RECORD"
csynth_design
ecg_report_summary lossy

puts "=== Module 3 complete ==="
exit 0
