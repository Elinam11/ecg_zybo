# Export the already-synthesized ecg_top IP. Does not re-run csim or csynth.
source [file join [file dirname [info script]] common.tcl]
open_project "$ECG_HLS_DIR/proj_top"
open_solution "solution1"
export_design -format ip_catalog \
    -display_name "ECG Adaptive Compressor" \
    -description "Real-time adaptive ECG compression (QRS lossless + non-QRS 5/3)" \
    -vendor "capstone" \
    -library "ecg" \
    -version "1.0"
puts "=== IP exported ==="
exit 0
