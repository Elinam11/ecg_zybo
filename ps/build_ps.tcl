# Build the bare-metal PS application from the exported XSA.
#
#   xsct ps/build_ps.tcl
#
# Produces ps/workspace/ecg_app/Debug/ecg_app.elf. Deletes and recreates the workspace
# each time: an incremental Vitis workspace that has seen a changed XSA is a reliable
# source of confusing failures.

set PS_DIR   [file normalize [file dirname [info script]]]
set REPO     [file normalize "$PS_DIR/.."]
set XSA      "$REPO/vivado/ecg_zybo.xsa"
set WS       "$PS_DIR/workspace"

if {![file exists $XSA]} {
    error "No XSA at $XSA. Run: vivado -mode batch -source vivado/build_vivado.tcl"
}

file delete -force $WS
file mkdir $WS
setws $WS

puts "Creating platform from $XSA"
platform create -name ecg_platform -hw $XSA -proc ps7_cortexa9_0 -os standalone \
    -out $WS
platform generate

app create -name ecg_app -platform ecg_platform -domain standalone_ps7_cortexa9_0 \
    -template {Empty Application(C)}

importsources -name ecg_app -path "$PS_DIR/src" -soft-link

# ecg_hw.h pulls in the generated constants header, which is the single definition of
# the frame format shared with the reference model.
app config -name ecg_app include-path "$REPO/hls/src"

# The exported HLS driver, when present, supplies the authoritative AXI4-Lite offsets.
set drv_dir "$REPO/hls/proj_top/solution1/impl/ip/drivers"
if {[file isdirectory $drv_dir]} {
    foreach d [glob -nocomplain -directory $drv_dir *] {
        if {[file isdirectory "$d/src"]} {
            app config -name ecg_app include-path "$d/src"
            puts "  using HLS driver headers from $d/src"
        }
    }
}

app config -name ecg_app build-config debug
app build -name ecg_app

set elf "$WS/ecg_app/Debug/ecg_app.elf"
if {![file exists $elf]} {
    error "Build reported success but $elf is missing"
}
puts "built $elf"
exit 0
