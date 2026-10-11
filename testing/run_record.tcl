# Prepare one record and start the core only after the host is listening.
#
#   xsct testing/run_record.tcl -record 101 -ready C:/tmp/ecg_ready
#
# Does: stop, rst -processor, ps7_init, ps7_post_config, memmap,
#       mwr data/coe/<record>.bin, dow. Then waits for -ready and issues con.
# Does not open the COM port.

set KIT  [file normalize [file dirname [info script]]]
set REPO [file normalize "$KIT/.."]

set opt_record     "100"
set opt_program    0
set opt_bit        "$KIT/ecg_system_wrapper.bit"
set opt_xsa        "$KIT/ecg_system_wrapper.xsa"
set opt_elf        "$KIT/ecg_app.elf"
set opt_ps7        "$KIT/ps7_init.tcl"
set opt_coe_dir    "$REPO/data/coe"
set opt_bram_base  0x40000000
set opt_ready      ""
set opt_prepared   ""
set opt_wait_ms    120000

for {set i 0} {$i < [llength $argv]} {incr i} {
    switch -- [lindex $argv $i] {
        "-record"     { incr i; set opt_record  [lindex $argv $i] }
        "-bit"        { incr i; set opt_bit     [lindex $argv $i] }
        "-xsa"        { incr i; set opt_xsa     [lindex $argv $i] }
        "-elf"        { incr i; set opt_elf     [lindex $argv $i] }
        "-ps7"        { incr i; set opt_ps7     [lindex $argv $i] }
        "-coe_dir"    { incr i; set opt_coe_dir [lindex $argv $i] }
        "-ready"      { incr i; set opt_ready    [lindex $argv $i] }
        "-prepared"   { incr i; set opt_prepared [lindex $argv $i] }
        "-wait_ms"    { incr i; set opt_wait_ms [lindex $argv $i] }
        "-program"    { set opt_program 1 }
        default       { puts "WARNING: ignoring [lindex $argv $i]" }
    }
}

if {$opt_ready eq ""} {
    error "-ready <path> is required so con is issued only after serial_rx is listening"
}

if {![file exists $opt_elf]} {
    set fallback "$REPO/ps/workspace/ecg_app/Debug/ecg_app.elf"
    if {[file exists $fallback]} {
        set opt_elf $fallback
    }
}

set sample_bin [file join $opt_coe_dir "${opt_record}.bin"]
if {![file exists $sample_bin]} {
    error "missing sample store $sample_bin"
}
if {![file exists $opt_ps7]} {
    error "missing $opt_ps7"
}
if {![file exists $opt_elf]} {
    error "missing ELF (looked at $KIT/ecg_app.elf and ps/workspace/ecg_app/Debug/ecg_app.elf)"
}

proc ecg_select_apu {} {
    if {![catch {targets -set -filter {name =~ "APU*"}}]} { return }
    targets -set -filter {target_id == 1}
}

proc ecg_select_cpu {} {
    if {![catch {targets -set -filter {name =~ "*Cortex-A9 MPCore #0*"}}]} { return }
    if {![catch {targets -set -filter {name =~ "*Cortex-A9*#0*"}}]} { return }
    targets -set -filter {target_id == 2}
}

proc ecg_select_fpga {} {
    if {![catch {targets -set -filter {name =~ "xc7z010*"}}]} { return }
    targets -set -filter {name =~ "*PL*" || name =~ "xc7z*"}
}

connect

ecg_select_apu
if {[file exists $opt_xsa]} {
    if {[catch {loadhw $opt_xsa} err]} {
        puts "WARNING: loadhw failed ($err); continuing with memmap"
    }
}

if {$opt_program} {
    if {![file exists $opt_bit]} {
        error "No bitstream at $opt_bit"
    }
    puts "Programming $opt_bit"
    ecg_select_fpga
    fpga -file $opt_bit
}

ecg_select_cpu
catch {stop}
rst -processor
source $opt_ps7
ps7_init
ps7_post_config
memmap -addr 0x40000000 -size 0x40000

set words [expr {[file size $sample_bin] / 4}]
puts "Loading record $opt_record ($words words) from $sample_bin"
mwr -bin -file $sample_bin $opt_bram_base $words
set probe [lindex [mrd -value $opt_bram_base 1] 0]
puts [format "  first word at 0x%08X reads back 0x%08X" $opt_bram_base $probe]

puts "Downloading $opt_elf"
dow $opt_elf

puts "ECG_PREPARED record=$opt_record"
flush stdout
if {$opt_prepared ne ""} {
    set _pf [open $opt_prepared w]
    puts $_pf $opt_record
    close $_pf
}

set waited 0
while {![file exists $opt_ready]} {
    after 200
    incr waited 200
    if {$waited >= $opt_wait_ms} {
        error "timeout waiting for host listener ($opt_ready)"
    }
}
file delete -force $opt_ready

puts "ECG_CON record=$opt_record"
con
after 500
puts "ECG_RUNNING record=$opt_record"
exit 0
