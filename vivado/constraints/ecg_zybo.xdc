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
