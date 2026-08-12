# Verification Status

What has actually been run, and what is authored but still waiting on tools or
hardware. No Xilinx toolchain and no board were available on the development machine,
so every gate below that needs one is listed explicitly rather than assumed.

## Closed

| Gate | Evidence | How to reproduce |
|---|---|---|
| Reference model meets all targets on all 46 MLII records | [verification_matrix.md](verification_matrix.md) — 46/46 pass, worst CR 5.29, worst PRD 1.66%, PRD on QRS exactly 0% everywhere | `python python/scripts/verify_matrix.py` |
| Decoder is the exact inverse of the encoder | 134 tests pass | `pytest python/tests` |
| HLS C++ core is bit-exact against the golden vectors | all five testbenches pass on records 100, 101, 119, 203, 208, 233 | `python hls/build_native.py --records 100 101 119 203 208 233` |
| TLAST appears exactly once, on the final word | asserted in `tb_top`, `tb_lossless`, `tb_lossy`, `tb_packer` | as above |
| Coder-to-packer FIFO produces the same bytes as a direct call | `tb_packer` token-protocol case | as above |
| UART transport framing survives boot chatter and truncation | `python/tests/test_transport.py` | `pytest python/tests/test_transport.py` |
| Host receiver decodes a frame identically to the reference model | loopback reports `BIT-IDENTICAL` | `python python/scripts/serial_rx.py --loopback 100` |
| Block RAM budget fits the device | [resource_budget.md](resource_budget.md) — 57 of 60 BRAM36 | `python python/scripts/resource_budget.py` |

## Open: needs Vitis HLS

| Gate | Script | What to check |
|---|---|---|
| C-simulation under Vitis matches the native build | `vitis_hls -f hls/tcl/run_all.tcl` | every `csim_design` passes |
| Synthesis meets 100 MHz and the resource budget | same | LUT/FF/DSP from `csynth`; BRAM should come in at 4 blocks for the IP, matching the budget |
| DATAFLOW region does not deadlock | same | no co-simulation hang; check the dataflow-checker warnings in the log |
| RTL reproduces the golden frame | same (`cosim_design` on a 30 s excerpt) | co-simulation passes |
| IP exports cleanly | same (`export_design`) | `hls/proj_top/solution1/impl/ip/component.xml` exists |

The highest-risk item here is the DATAFLOW region in `ecg_top`. It is structured to be
canonical — one producer, one consumer, a single FIFO declared inside the region, and
only the sink writes the AXI4-Lite status registers — but that structure has not been
checked by the tool.

## Open: needs Vivado

| Gate | Script | What to check |
|---|---|---|
| Block design builds and validates | `vivado -mode batch -source vivado/build_vivado.tcl -tclargs -bd_only` | `validate_bd_design` clean |
| Implementation meets timing | full build | setup WNS >= 0; the script errors out otherwise |
| Utilisation matches the budget | full build | `data/reports/utilization_100.rpt` |
| XSA exports | full build | `vivado/ecg_zybo.xsa` |

Two parts of the block design deserve attention on the first run:

- **The asymmetric sample store.** `blk_mem_gen` is configured true dual port with a
  32-bit port A and a 16-bit port B. Confirm the generated core reports 108,000 words on
  port B, and that the `<record>_x32.coe` initialises it in the expected order (sample
  2i in the low half-word).
- **The HLS BRAM interface name.** `create_bd.tcl` connects
  `ecg_top_0/ecg_bram_PORTA`. If the exported IP names that interface differently, the
  connection fails immediately and loudly; correct the name and re-run.

## Open: needs the board

| Gate | Script | What to check |
|---|---|---|
| Firmware builds | `xsct ps/build_ps.tcl` | ELF produced |
| Status register offsets are right | first hardware run | `ps/src/ecg_hw.h` falls back to the standard HLS layout; once the IP is exported, the generated `xecg_top_hw.h` takes precedence automatically |
| Hardware frame is byte-identical to golden | `python python/scripts/serial_rx.py --port <PORT> --record 100` | `BIT-IDENTICAL` |
| Latency measured | same | reported in PL cycles; the budget predicts about 833,000 |
| All records pass on hardware | `python python/scripts/verify_matrix.py --mode hardware --port <PORT>` | 46/46 byte-identical |

[hardware_bringup.md](hardware_bringup.md) has the fault-isolation guide for when one of
these fails.
