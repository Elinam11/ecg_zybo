# Hardware Bring-Up Runbook

Everything up to this point is verified in simulation. This is the sequence that turns
that into a working board, and the checks that tell you which step broke when it does
not work the first time.

Target: Zybo Z7-10 (XC7Z010-1CLG400C), Vivado / Vitis 2023.2.

## Prerequisites

- Digilent board files installed (`vivado-boards/new/board_files` copied into
  `<Vivado>/data/boards/board_files`). The build stops with an explicit error without
  them, because the Zynq preset they carry defines this board's DDR3 timing.
- One micro-USB cable on the board's **PROG** connector. It carries JTAG *and* the PS
  UART, and the compressed frame comes out of the PS UART, so no second adapter and no
  Pmod wiring are needed. The `axi_uartlite` on Pmod JE1 is still in the block design
  but the firmware no longer drives it.
- Nothing else may hold that COM port while `serial_rx.py` runs. A terminal left open on
  it (PuTTY, Tera Term, the Vitis serial monitor) either blocks the port outright or eats
  the frame bytes the receiver is waiting for.
- MIT-BIH data downloaded and artefacts generated:

```bash
python python/scripts/download_mitdb.py
python python/scripts/gen_coe.py
python python/scripts/run_reference.py 100 --golden
```

## Build sequence

```bash
# 1. HLS: C-sim vs golden, synthesis, co-simulation, IP export
vitis_hls -f hls/tcl/run_all.tcl

# 2. Vivado: block design, implementation, bitstream, XSA
vivado -mode batch -source vivado/build_vivado.tcl

# 3. Vitis: bare-metal application
xsct ps/build_ps.tcl

# 4. Program and run record 100
xsct ps/run_hw.tcl -record 100

# 5. Capture and score on the host. --port is the Digilent COM port that the PROG
#    micro-USB creates; start this before step 4 so no bytes are missed.
python python/scripts/serial_rx.py --port COM5 --record 100 --save capture_100.bin
```

## One link carries both the log text and the frame

The firmware prints its status lines *before* the frame, sends the sync word, the length,
the latency, the header and the payload with no console output in between, and only then
prints `done`. `serial_rx.py` resynchronises on the sync word (`0x45434731`) and stops
after the length it was given, so the banner ahead of the frame and the `done` line after
it are both ignored.

That quiet window is the whole reason this works on one wire. Anyone editing
`ps/src/main.c` must keep it: a single `xil_printf` between the sync word and the end of
the payload lands *inside* the frame the host is counting out, and the capture is
unrecoverable.

Text and frame both leave through the BSP's stdout, so the banner arriving on the Digilent
COM port is also the proof that stdout is the PS UART (`ps7_uart_1`) rather than the
leftover `axi_uartlite`. If a banner ever appears on a JE1 adapter instead, the platform
bound stdout to the wrong peripheral: set the domain's stdout with `domain config -stdout
ps7_uart_1` in XSCT, regenerate the platform, and rebuild the ELF.

## Pass criteria for record 100

| Check | Expected | Where it comes from |
|---|---|---|
| frame matches golden | `BIT-IDENTICAL` | `serial_rx.py` compares against `pipeline.encode_record` |
| checksum | `OK` | additive checksum in header word 3 |
| PRD on QRS | exactly 0.000% | lossless path |
| PRD overall (baseline) | < 2% | `config.PRD_GATE_METRIC` |
| CR | > 5 | 9.59 in the reference model for record 100 |
| latency | reported in PL cycles | A9 global timer around `ap_start` .. `ap_done` |

`BIT-IDENTICAL` is the check that matters. CR and PRD can look plausible while the
hardware is subtly wrong; a byte-exact match cannot.

## If it does not work

Work down this list; each step isolates one layer.

**Nothing on the port at all.** Open the Digilent COM port at 115200 8N1 in a terminal
and re-run the board: the banner should appear the moment the ELF starts. If it does not,
the firmware is not running, so the problem is upstream of any UART question — check that
JP5 is set to JTAG, that `dow` succeeded and that `con` was issued. Close the terminal
again before running `serial_rx.py`; only one process can own the port.

**Banner, then a hang.** The core never asserts `ap_done`. The usual cause is the DMA not
being armed, so the core blocks writing to a full stream. Check that
`XAxiDma_SimpleTransfer` returned success and that `s2mm_introut` is connected.

**`serial_rx.py` reports "no sync word".** The receiver saw bytes, or nothing, but never
the frame. Now that text and frame share one link the order of suspicion is:

- another process holds the port, so the frame went to it instead;
- the run failed before transmitting. Watch the same port in a terminal instead of
  `serial_rx.py` and read the `run:` / `ERROR:` lines the firmware prints first;
- both ends disagree on 115200 8N1, or `ECG_UART_SYNC` in `hls/src/ecg_constants.h` was
  not regenerated after a `config.py` change
  (`python python/scripts/export_hls_constants.py`).

**Checksum mismatch, frame length plausible.** Something got into the frame that is not
frame. Either bytes were dropped — the PS UART has no flow control, so this is usually the
host's receive buffer, which `--timeout` and a `--save` / `--file` replay will tell you
apart — or console text was injected mid-frame, which means a `xil_printf` has been added
inside the quiet window in `run_once`.

**Frame decodes but differs from golden.** This is the interesting failure, and the
first divergent word tells you where to look:

- *word 0-3 (header)*: the PS built the header wrong; check the status register offsets
  in `ps/src/ecg_hw.h` against the generated `xecg_top_hw.h`.
- *first payload word*: the sample store contents differ from what Python compressed.
  Re-run `xsct ps/run_hw.tcl -record 100` and check the read-back probe it prints, and
  confirm the half-word packing (sample 2i in the low half of each 32-bit word).
- *a word deep into the payload*: a genuine algorithmic divergence between the RTL and
  the reference model. Reproduce it in co-simulation, which is far faster to debug than
  hardware: `vitis_hls -f hls/tcl/top_integrate.tcl`.

**Everything matches but latency looks wrong.** `XTime` counts at half the CPU
frequency; `COUNTS_PER_SECOND` from the BSP already accounts for that. A latency far
above the ~8 ms budget means the core is being back-pressured, which points at the DMA
rather than the compression engine.

## Rebuilding only the firmware

Which UART the frame leaves on is decided entirely in `ps/src/main.c`, so moving it cost
one ELF rebuild and no implementation run. The same three commands re-test any firmware
change against an already-programmed device:

```bash
# 1. Rebuild ecg_app.elf; the bitstream and the XSA are untouched
xsct ps/build_ps.tcl

# 2. Program the existing .bit and download the new ELF
xsct ps/run_hw.tcl -record 100

# 3. Capture on the Digilent COM port, with no terminal open on it
python python/scripts/serial_rx.py --port COM5 --record 100 --save capture_100.bin
```

Record 100 must still come back `BIT-IDENTICAL`. The bytes the PL produces did not
change, only the wire they leave on, so anything else is a transport problem rather than
a compression one — and a saved `capture_100.bin` can be re-scored offline with
`--file` to prove the decode side independently of the link.

When handing a kit to someone else, ship the rebuilt `ecg_app.elf` with the existing
`ecg_system_wrapper.bit` and `ecg_zybo.xsa`. A stale ELF is the one failure that looks
like broken hardware: it transmits on Pmod JE1, so the PROG COM port shows the banner and
then nothing at all.

## Swapping records

The sample store is a true dual-port BRAM: the core reads port B, and port A is on the
AXI BRAM controller. `xsct ps/run_hw.tcl -record 208` writes
`data/coe/208.bin` into it over JTAG and re-runs. No re-synthesis, no bitstream
patching, no rebuild of anything.

The `.coe` still initialises the memory at configuration time, so a board programmed
from flash runs record 100 with no host involvement at all.
