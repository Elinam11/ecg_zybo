## ECG compression system - Zybo Z7-10 constraints
##
## Only the PL-side pins the design actually uses. Everything else (DDR, MIO, the PS
## clock) comes from the Zynq processing-system block, which the board preset
## configures, so it must not be constrained here.
##
## Derived from Zybo-Z7-Master.xdc.

## Pmod Header JE, pin 1: UART transmit to the host.
set_property -dict { PACKAGE_PIN V12  IOSTANDARD LVCMOS33 } [get_ports { tx_line }]

## The UART is asynchronous to the 100 MHz PL clock at the pin, and UARTLite
## oversamples internally, so the output has no meaningful timing relationship to
## anything else in the design.
set_false_path -to [get_ports tx_line]

## Keep the HP0 interconnect next to the PS (left side of XC7Z010). Area_Explore
## otherwise parks SmartConnect at SLICE_X39Y98 and S_AXI_HP0_AWSIZE misses by
## ~0.17 ns of route delay while the ECG core itself is already positive-slack.
create_pblock pblock_ps_hp
resize_pblock pblock_ps_hp -add {SLICE_X0Y0:SLICE_X9Y49}
add_cells_to_pblock pblock_ps_hp [get_cells -quiet -hier -filter {NAME =~ *smartconnect_hp*}]
set_property SNAPPING_MODE ON [get_pblocks pblock_ps_hp]
