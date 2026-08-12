#### **Steps** 

**Hardware**

1. Zybo Z7-10 (same part as your build).

2\. USB JTAG/UART to the board (PS console on micro-USB).

3\. Separate 3.3 V USB-UART: TX from adapter to Pmod JE pin 1, GND to JE ground. Data is not the PS console UART.

4\. Vivado/Vitis 2023.2 tools with xsct on PATH (matching your export is safest).



**Host setup**

pip install numpy pyserial

\# unpack the zip; cd into it



**Run**

1. Note the USB-UART COM port (e.g. COM5).



2\. Start capture first:

python python/scripts/serial\_rx.py --port COM5 --record 100 --save capture\_100.bin



3\. In another shell, program and run (adjust paths):

xsct ps/run\_hw.tcl -record 100 -bit path/to/ecg\_system\_wrapper.bit -elf path/to/ecg\_app.elf

If bitstream is inside the XSA only, extract/program the .bit from the XSA, or use Hardware Manager to program the bit, then:

xsct ps/run\_hw.tcl -record 100 -no\_program -elf path/to/ecg\_app.elf



**Pass**

* serial\_rx prints BIT-IDENTICAL
* checksum OK, QRS PRD = 0%, overall PRD < 2%, CR > 5

If nothing on the data UART: check JE1 wiring/GND; PS console on micro-USB should still show the firmware banner if the ELF is running.

