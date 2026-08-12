# Module 4: bitstream packer. Self-contained testbench, no golden vectors needed.
#   vitis_hls -f hls/tcl/module_packer.tcl
#
# Top must be ecg_packer_module_top (not ecg_bitpack_put): ap_axiu stream ports are
# only legal on AXI-Stream interfaces (HLS 214-208).
source [file join [file dirname [info script]] common.tcl]

puts "=== Module 4: packer ==="
# The packer testbench also exercises header assembly, so the whole core is compiled.
ecg_open_module packer ecg_packer_module_top \
    {bitpack.cpp detect.cpp lossless.cpp lossy.cpp ecg_top.cpp} tb_packer.cpp

csim_design
csynth_design
ecg_report_summary packer

puts "=== Module 4 complete ==="
exit 0
