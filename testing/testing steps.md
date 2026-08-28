#### **Steps**

**# Zybo Z7-10 board test (from .bit + .elf)**

Target: **Zybo Z7-10 (xc7z010)**. Tools: **Vitis/XSCT 2023.2** (or matching your export).

This flow assumes you already have a built bitstream and bare-metal ELF. You do **not** need to re-run Vivado implementation or Vitis app create on the test PC if these files are provided.

**---**

**## 1. Files you need in one folder (e.g.** `testing/`**)**

| File | Purpose |

|------|---------|

| `ecg_system_wrapper.bit` | Program the PL |

| `ecg_app.elf` | PS software. Must be a build that sends the frame on the **PS UART** — an older ELF transmits on Pmod JE1 and the PROG COM port then shows the banner and nothing else. Rebuild with `xsct ps/build_ps.tcl`; the `.bit` and `.xsa` do not change |

| `ecg_zybo.xsa` | Address map for XSDB `loadhw`) |

| `ps7_init.tcl` | Defines `ps7_init` / `ps7_post_config` |

| `100.bin` | Record 100 samples for BRAM (216000 bytes) — optional if bit was built with record-100 `.coe` |

| `python/` tree | At least `ecg_ref/`, `scripts/serial_rx.py`, `scripts/_bootstrap.py` |

| MIT-BIH record 100 (via `download_mitdb.py`) | So `serial_rx` can score vs golden |

Also useful: this instructions file.

**Do not use placeholder paths** like `path/to/...`. Use real full paths.

**---**

**## 2. Hardware**

1. Power the Zybo (adapter if required).
2. **JP5 = JTAG** boot mode (Digilent Zybo Z7 ref manual).
3. USB cable on the board **PROG** connector → PC. This one cable is everything: JTAG, the text log **and** the compressed data.

- No Pmod JE1 wiring and no USB-UART adapter are needed.

1. Close Vivado Hardware Manager / other XSDB sessions that might hold the cable.
2. Note the COM port in Device Manager: the **Digilent** / PROG UART (e.g. COM5). That single port carries both the firmware log text and the compressed frame, so it is the `--port` value for `serial_rx.py`.
3. Keep serial terminals (PuTTY, Tera Term, the Vitis serial monitor) **closed** on that port while `serial_rx.py` runs. Only one process can own a COM port, and whoever owns it receives the frame.

---

**## 3. Host software**

```bat

pip install numpy pyserial



## **Program and run (XSDB)**

- Note the Digilent / PROG COM port (e.g. COM5).

- Start capture first, and leave no terminal open on that port:

python python/scripts/serial_rx.py --port COM5 --record 100 --save capture_100.bin



- From a Vitis commadnd prompt:

xsdb

- Then paste (edit paths to your kit folder):

set KIT C:/Users/elina/Moved_files/Capstone/ECG_Code/testing

connect

targets



# APU only (parent) — required for loadhw

targets -set -filter {target_id == 1}

loadhw $KIT/ecg_system_wrapper.xsa



# Program FPGA

targets -set -filter {name =~ "xc7z010"}
fpga -file $KIT/ecg_system_wrapper.bit




# CPU

targets -set -filter {target_id == 2}

stop

rst -processor

source $KIT/ps7_init.tcl

ps7_init

ps7_post_config



# Allow JTAG access to PL BRAM @ 0x40000000

memmap -addr 0x40000000 -size 0x40000



# Load record 100 into sample store (skip if using .coe-only bit)

mwr -bin -file $KIT/100.bin 0x40000000 [expr {216000/4}]

mrd 0x40000000 1

dow $KIT/ecg_app.elf



**- Before** `con`**:** start the host capture (section 5), then:

con

You should see: `Successfully downloaded ... ecg_app.elf` and `Running`.



### **If something fails**


| **Error**                              | **Fix**                                                                                          |
| -------------------------------------- | ------------------------------------------------------------------------------------------------ |
| No `APU*` / only DAP error + `xc7z010` | Power, JP5=JTAG, PROG USB, close other tools, power-cycle                                        |
| `invalid command name ps7_init`        | `source` the provided `ps7_init.tcl`                                                             |
| `loadhw` → Invalid target              | Select **APU (id 1)**, not Cortex-A9 #0 (id 4)                                                   |
| `mwr` → PL AXI / memory map            | `loadhw` xsa + `memmap -addr 0x40000000 -size 0x40000` after `ps7_post_config`; `stop` CPU first |
| Filter matches two targets             | Use `targets -set -filter {target_id == 1}` or `== 4`                                            |




## **5. Receive compressed data + decompress / score**

The board sends **compressed** bytes on the **PS UART**, which is the Digilent / PROG micro-USB COM port at 115200 8N1 — the same port as the text log.

The firmware prints its `run: samples=...` lines first, then transmits the frame with no text in between, then prints `done`. `serial_rx.py` skips the leading text (it hunts for the sync word) and stops at the end of the frame, so sharing the port is safe as long as **only** `serial_rx.py` is listening.

Decompression is done on the **PC** by `serial_rx.py`.

**Start this before** `con` (or re-`dow`/`con` if you missed the frame):

cd C:\Users\YOU\Desktop\ECG_Compression_FPGA

python python/scripts/serial_rx.py --port COM5 --record 100 --save capture_100.bin

Replace `COM5` with the **Digilent / PROG** COM port.

To read the `xil_printf` log instead, open that COM port at 115200 in a serial terminal and re-run — but not at the same time as `serial_rx.py`, since the two would fight over the port.

### **Pass criteria (record 100)**

- `vs golden BIT-IDENTICAL`
- checksum OK
- PRD QRS = 0%
- overall (baseline) PRD < 2%
- CR > 5

Replay a saved capture later:

python python/scripts/serial_rx.py --file capture_100.bin --record 100

---

## **6. Data path (what the single cable carries)**


| **Link**              | **Connector**  | **Content**                                     |
| --------------------- | -------------- | ----------------------------------------------- |
| JTAG                  | PROG micro-USB | Program bit/elf, download and start the ELF     |
| PS UART (same cable)  | PROG micro-USB | Text log, then the compressed frame (binary)    |
| Host Python           | —              | Decode → reconstructed ECG + metrics            |


The FPGA does **not** stream decompressed samples over UART.

If nothing arrives: confirm you are on the Digilent / PROG COM port at 115200 8N1, that no other program holds it, and that the ELF is actually running (a terminal on that port shows the banner as soon as it starts).
```

