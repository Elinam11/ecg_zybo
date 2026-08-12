# Module 2: lossless Golomb-Rice QRS coder.
#   vitis_hls -f hls/tcl/module_lossless.tcl [-tclargs <record>]
source [file join [file dirname [info script]] common.tcl]

puts "=== Module 2: lossless (record $ECG_RECORD) ==="
ecg_open_module lossless ecg_encode_qrs_top {bitpack.cpp lossless.cpp} tb_lossless.cpp

csim_design -argv "$ECG_RECORD"
csynth_design
ecg_report_summary lossless

puts "=== Module 2 complete ==="
exit 0
