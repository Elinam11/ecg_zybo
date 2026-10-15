# Run the entire HLS flow headless: every module, then the top-level integration.
#
#   vitis_hls -f hls/tcl/run_all.tcl               # record 100
#   vitis_hls -f hls/tcl/run_all.tcl -tclargs 208  # a different record
#
# Any csim assertion failure aborts the run with a non-zero exit status, so this is
# safe to use as a CI gate.
source [file join [file dirname [info script]] common.tcl]

puts "###############################################################"
puts "# ECG compression: full HLS flow"
puts "#   part   : $ECG_PART"
puts "#   clock  : $ECG_CLK_PERIOD ns"
puts "#   record : $ECG_RECORD"
puts "###############################################################"

# Regenerate ecg_constants.h so the hardware can never be built against stale
# constants after config.py changes.
if {[catch {
    exec [ecg_python] "$ECG_REPO/python/scripts/export_hls_constants.py"
} result]} {
    puts "WARNING: could not regenerate ecg_constants.h: $result"
    puts "         (set ECG_PYTHON to your interpreter if this matters)"
} else {
    puts $result
}

# ---- Module-level: C-simulation vs golden vectors, then synthesis ----
puts "\n### Module 1: detect ###"
ecg_open_module detect ecg_detect_triggers {detect.cpp} tb_detect.cpp
csim_design -argv "$ECG_RECORD"
csynth_design
ecg_report_summary detect

puts "\n### Module 2: lossless ###"
ecg_open_module lossless ecg_encode_qrs_top {bitpack.cpp lossless.cpp} tb_lossless.cpp
csim_design -argv "$ECG_RECORD"
csynth_design
ecg_report_summary lossless

puts "\n### Module 3: lossy ###"
ecg_open_module lossy ecg_encode_nonqrs_top {bitpack.cpp lossy.cpp} tb_lossy.cpp
csim_design -argv "$ECG_RECORD"
csynth_design
ecg_report_summary lossy

puts "\n### Module 4: packer ###"
# ap_axiu is only legal on AXI-Stream interface ports (HLS 214-208); do not set_top
# ecg_bitpack_put.
ecg_open_module packer ecg_packer_module_top \
    {bitpack.cpp detect.cpp lossless.cpp lossy.cpp ecg_top.cpp} tb_packer.cpp
csim_design
csynth_design
ecg_report_summary packer

# ---- Top level: whole-frame bit-exactness, cosim, IP export ----
puts "\n### Top level: integration ###"
ecg_open_module top ecg_top \
    {bitpack.cpp detect.cpp lossless.cpp lossy.cpp ecg_top.cpp} tb_top.cpp
csim_design -argv "$ECG_RECORD"
csynth_design
ecg_report_summary top
if {[ecg_ensure_cosim_golden]} {
    cosim_design -rtl verilog -trace_level none -argv "$ECG_COSIM_RECORD"
} else {
    puts "SKIPPING cosim: golden vectors unavailable"
}
export_design -format ip_catalog \
    -display_name "ECG Adaptive Compressor" \
    -description "Real-time adaptive ECG compression (QRS lossless + non-QRS bior4.4)" \
    -vendor "capstone" -library "ecg" -version "1.0"

puts "\n###############################################################"
puts "# All HLS stages passed. IP exported from hls/proj_top."
puts "###############################################################"
exit 0
