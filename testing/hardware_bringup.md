# Hardware Bring-Up Runbook

Everything up to this point is verified in simulation. This is the sequence that turns
that into a working board, and the checks that tell you which step broke when it does
not work the first time.

Target: Zybo Z7-10 (XC7Z010-1CLG400C), Vivado / Vitis 2023.2.

## Prerequisites

- Digilent board files installed (`vivado-boards/new/board_files` copied into
  `<Vivado>/data/boards/board_files`). The build stops with an explicit error without
  them, because the Zynq preset they carry defines this board's DDR3 timing.
- A 3.3 V USB-UART adapter on Pmod **JE pin 1** (`tx_line`, package pin V12) and its
  ground on a JE ground pin. This is the data link; the board's own micro-USB carries
  the PS console, which is separate.
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

# 5. Capture and score on the host (start this before step 4 on a re-run)
python python/scripts/serial_rx.py --port COM5 --record 100 --save capture_100.bin
```

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

**Nothing on the UART at all.** Confirm the PS console (micro-USB, 115200) shows the
banner. If the banner is there, the firmware is running and the problem is the PL UART:
check the JE1 wiring and that the adapter's ground is connected. If the banner is
absent, the ELF is not running: re-run `xsct ps/build_ps.tcl`.

**Console banner, then a hang.** The core never asserts `ap_done`. The usual cause is
the DMA not being armed, so the core blocks writing to a full stream. Check that
`XAxiDma_SimpleTransfer` returned success and that `s2mm_introut` is connected.

**`serial_rx.py` reports "no sync word".** The PS is transmitting but the byte stream
is not what the host expects. Confirm both ends agree on 115200 8N1, and that
`ECG_UART_SYNC` in `hls/src/ecg_constants.h` was regenerated after any `config.py`
change (`python python/scripts/export_hls_constants.py`).

**Checksum mismatch, frame length plausible.** Bytes are being dropped on the wire.
UARTLite has no flow control, so this is almost always the host's receive buffer; raise
the timeout or capture with `--save` and replay with `--file`.

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

## Swapping records

The sample store is a true dual-port BRAM: the core reads port B, and port A is on the
AXI BRAM controller. `xsct ps/run_hw.tcl -record 208` writes
`data/coe/208.bin` into it over JTAG and re-runs. No re-synthesis, no bitstream
patching, no rebuild of anything.

The `.coe` still initialises the memory at configuration time, so a board programmed
from flash runs record 100 with no host involvement at all.
