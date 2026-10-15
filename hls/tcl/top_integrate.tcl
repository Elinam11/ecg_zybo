# Phase 3: top-level integration, co-simulation and IP export.
#
#   vitis_hls -f hls/tcl/top_integrate.tcl [-tclargs <record>]
#
# csim proves the complete frame is bit-identical to the Python golden bitstream;
# cosim proves the generated RTL reproduces that same frame.
source [file join [file dirname [info script]] common.tcl]

puts "=== Top-level integration (record $ECG_RECORD) ==="
ecg_open_module top ecg_top \
    {bitpack.cpp detect.cpp lossless.cpp lossy.cpp ecg_top.cpp} tb_top.cpp

# ---- C simulation: whole-frame bit-exactness ----
csim_design -argv "$ECG_RECORD"

# ---- Synthesis ----
csynth_design
ecg_report_summary top

# ---- RTL co-simulation ----
# Against a 30 s excerpt: the full record would take hours of RTL simulation while
# proving nothing extra, since every code path is already covered in 30 s.
#
# ecg_top packs inline (no DATAFLOW+AXIS). That avoids the historic cosim deadlock
# where the packer blocked on TREADY while the TB waited for ap_done.
if {[info exists ::env(ECG_SKIP_COSIM)] && $::env(ECG_SKIP_COSIM) ne "0"} {
    puts "SKIPPING cosim (ECG_SKIP_COSIM)"
} elseif {[ecg_ensure_cosim_golden]} {
    cosim_design -rtl verilog -trace_level none -argv "$ECG_COSIM_RECORD"
} else {
    puts "SKIPPING cosim: golden vectors unavailable"
}

# ---- Package for Vivado IP integrator ----
# Export is independent of cosim. Skipping it leaves Vivado on a stale IP whose
# BRAM estimate still overflows the XC7Z010 at validate_bd_design.
if {[info exists ::env(ECG_SKIP_EXPORT)] && $::env(ECG_SKIP_EXPORT) ne "0"} {
    puts "SKIPPING export_design (ECG_SKIP_EXPORT)"
} else {
    export_design -format ip_catalog \
        -display_name "ECG Adaptive Compressor" \
        -description "Real-time adaptive ECG compression (QRS lossless + non-QRS bior4.4)" \
        -vendor "capstone" \
        -library "ecg" \
        -version "1.0"
    puts "=== Top-level complete: IP exported ==="
}
exit 0
