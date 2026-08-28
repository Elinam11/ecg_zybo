# Load testing/100.bin into the sample BRAM one 32-bit word at a time.
# JTAG bursts on this AXI BRAM slave all hit address 0 (last file word wins).
# Usage, with Cortex-A9 #0 selected after ps7_post_config + memmap:
#   set KIT C:/Users/elina/Moved_files/Capstone/ECG_Code/testing
#   source $KIT/load_bram.tcl
#
# Takes several minutes. Do not interrupt. Dummy-read twice after it finishes.

if {![info exists KIT]} {
    error "set KIT to the testing/ folder before sourcing this script"
}

set ecg_bin  [file join $KIT 100.bin]
set ecg_base 0x40000000
set ecg_n    54000

if {![file exists $ecg_bin]} {
    error "missing $ecg_bin"
}

puts "smoke: two single-word writes"
mwr $ecg_base 0xA5A5A5A5
mwr [expr {$ecg_base + 4}] 0x5A5A5A5A
# Posted JTAG read: discard one, second print is [base]
mrd $ecg_base 1
set _probe [lindex [mrd -value $ecg_base 1] 0]
puts [format "  double-read [0x%08X] = 0x%08X (want 0xA5A5A5A5)" $ecg_base $_probe]
if {($_probe & 0xFFFFFFFF) != 0xA5A5A5A5} {
    error "single-word write to BRAM did not stick; rebuild the bitstream with 100_x32.coe instead"
}

set fp [open $ecg_bin r]
fconfigure $fp -translation binary
set data [read $fp]
close $fp
if {[string length $data] < $ecg_n * 4} {
    error "100.bin is too short"
}

puts "writing $ecg_n words (this is slow)..."
for {set n 0} {$n < $ecg_n} {incr n} {
    set off [expr {$n * 4}]
    binary scan [string range $data $off [expr {$off + 3}]] i w
    mwr [expr {$ecg_base + $off}] [expr {$w & 0xFFFFFFFF}]
    if {($n % 2000) == 0} {
        puts "  $n / $ecg_n"
    }
}
puts "done. double-read word 0 (second value must be 00070007):"
mrd $ecg_base 1
mrd $ecg_base 1
