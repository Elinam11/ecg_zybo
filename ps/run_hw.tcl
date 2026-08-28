# Program the board and run one compression pass.
#
#   xsct ps/run_hw.tcl                 # record 100, from the .coe already in the bitstream
#   xsct ps/run_hw.tcl -record 208     # overwrite the sample store over JTAG first
#   xsct ps/run_hw.tcl -no_program     # board already configured; just re-run
#
# Swapping records writes data/coe/<record>.bin straight into the sample store through
# the AXI BRAM controller. No re-synthesis, no bitstream patching: the memory is dual
# ported precisely so this is possible.

set PS_DIR [file normalize [file dirname [info script]]]
set REPO   [file normalize "$PS_DIR/.."]

set opt_record     "100"
set opt_program    1
set opt_bit        "$REPO/vivado/proj_ecg/ecg_zybo.runs/impl_1/ecg_system_wrapper.bit"
set opt_elf        "$PS_DIR/workspace/ecg_app/Debug/ecg_app.elf"
set opt_bram_base  0x40000000

for {set i 0} {$i < [llength $argv]} {incr i} {
    switch -- [lindex $argv $i] {
        "-record"     { incr i; set opt_record [lindex $argv $i] }
        "-bit"        { incr i; set opt_bit    [lindex $argv $i] }
        "-elf"        { incr i; set opt_elf    [lindex $argv $i] }
        "-no_program" { set opt_program 0 }
        default       { puts "WARNING: ignoring [lindex $argv $i]" }
    }
}

set sample_bin "$REPO/data/coe/${opt_record}.bin"

connect

# The PS must be initialised before any AXI access: ps7_init sets up the clocks, the
# DDR controller and the PL level shifters, without which a write to the BRAM
# controller silently goes nowhere.
targets -set -filter {name =~ "APU*"}
if {$opt_program} {
    if {![file exists $opt_bit]} {
        error "No bitstream at $opt_bit. Run: vivado -mode batch -source vivado/build_vivado.tcl"
    }
    puts "Programming $opt_bit"
    targets -set -filter {name =~ "*PL*" || name =~ "xc7z*"}
    fpga -file $opt_bit
}

targets -set -filter {name =~ "*Cortex-A9 MPCore #0*"}
rst -processor
ps7_init
ps7_post_config

if {[file exists $sample_bin]} {
    set bytes [file size $sample_bin]
    puts "Loading record $opt_record into the sample store ($bytes bytes)"
    mwr -bin -file $sample_bin $opt_bram_base [expr {$bytes / 4}]

    # Read one word back. A silent failure here (wrong address, PL not programmed,
    # level shifters still down) would otherwise show up much later as a corrupt frame.
    set probe [lindex [mrd -value $opt_bram_base 1] 0]
    puts [format "  first word at 0x%08X reads back 0x%08X" $opt_bram_base $probe]
} else {
    puts "No $sample_bin; running whatever the .coe initialised the BRAM with."
}

if {![file exists $opt_elf]} {
    error "No ELF at $opt_elf. Run: xsct ps/build_ps.tcl"
}
puts "Downloading $opt_elf"
dow $opt_elf
con

puts "Running. Capture the frame with (PORT = the Digilent COM port on this cable,"
puts "which carries the PS UART as well as JTAG; no other terminal may hold it):"
puts "  python python/scripts/serial_rx.py --port <PORT> --record $opt_record"
exit 0
