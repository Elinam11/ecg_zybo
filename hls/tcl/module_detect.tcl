# Module 1: QRS detection. C-simulation against golden vectors, then synthesis.
#   vitis_hls -f hls/tcl/module_detect.tcl [-tclargs <record>]
source [file join [file dirname [info script]] common.tcl]

puts "=== Module 1: detect (record $ECG_RECORD) ==="
ecg_open_module detect ecg_detect_triggers {detect.cpp} tb_detect.cpp

csim_design -argv "$ECG_RECORD"
csynth_design
ecg_report_summary detect

puts "=== Module 1 complete ==="
exit 0
