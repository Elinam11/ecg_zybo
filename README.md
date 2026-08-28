# FPGA Adaptive ECG Compression

Real-time adaptive ECG compression on a Zybo Z7-10 (Zynq XC7Z010). QRS complexes are
compressed losslessly so the diagnostically critical part of the signal is reconstructed
exactly; the baseline between beats goes through a wavelet path where the bits can be
spent more cheaply.

Measured on all 46 MLII-bearing MIT-BIH records with one frozen configuration:

| | Target | Achieved |
|---|---|---|
| PRD on QRS | 0% | **0%**, every record |
| PRD overall | < 2% | 1.66% worst case, 1.07% mean |
| Compression ratio | > 5:1 | **5.29 worst case**, 8.39 mean |
| Latency | real time | ~8 ms per 5-minute record (~36,000x real time) |

Full per-record results: [docs/verification_matrix.md](docs/verification_matrix.md).

## How it works

```
   MIT-BIH record
        │
        ▼  offline, Python
   bandpass + 16-bit quantisation  ──►  .coe / .bin  ──►  PL block RAM
                                                              │
   ┌──────────────────────────────────────────────────────────┘
   ▼  on the FPGA
   first-order IIR bandpass ──► Teager-Kaiser energy ──► adaptive threshold
        │
        ├── QRS window (R-29 .. R+43)  ──► 2nd-order predictor + Golomb-Rice   (lossless)
        └── everything else            ──► 4-level Haar DWT + threshold + RLE  (lossy)
                                                │
                                    bit packer ──► AXI4-Stream ──► DMA ──► DDR
                                                                            │
                                                            PS ──► UART ──► host
```

The distortion budget is deliberately asymmetric. Every QRS complex comes back bit for
bit; the aggressive detail-band thresholds apply only between beats, which is where the
compression comes from.

## Repository layout

```
python/ecg_ref/      reference model: the definition of the algorithm
python/scripts/      data prep, calibration, golden vectors, host receiver, reports
python/tests/        unit and integration tests (pytest)
hls/src/             the synthesisable C++ core
hls/tb/              C-simulation testbenches, bit-exact against golden vectors
hls/tcl/             headless Vitis HLS flow
hls/build_native.py  runs the same testbenches under g++, no Xilinx tools needed
vivado/              block design and headless implementation flow
ps/                  bare-metal Cortex-A9 firmware and its build/run scripts
docs/                verification matrix, resource budget, bring-up runbook
data/                downloaded records and generated artefacts (gitignored)
```

## The reference model is the specification

`python/ecg_ref/` is not a prototype that the hardware approximates. It is the
definition of the bitstream, and the HLS core is required to reproduce it bit for bit.
Every stage is integer arithmetic with explicit shifts for exactly this reason, and
`hls/src/ecg_constants.h` is generated from `config.py` so the two cannot drift apart.

This is what makes the whole thing debuggable: a divergence shows up as a mismatched
32-bit word in a testbench that runs in seconds, rather than as a wrong number coming
out of a serial port.

## Getting started

```bash
pip install -r requirements.txt

python python/scripts/download_mitdb.py        # fetch MIT-BIH from PhysioNet
python python/scripts/gen_coe.py               # BRAM initialisation files
python python/scripts/run_reference.py 100 --golden
pytest python/tests
```

`run_reference.py --golden` writes `data/golden/<record>/`, which every hardware
testbench compares against.

### Verify the C++ core without Xilinx tools

```bash
python hls/build_native.py --records 100 101 119 203 208 233
```

Compiles the synthesisable sources with g++ (`hls::stream` is shimmed, the arithmetic is
untouched) and asserts bit-exactness against the golden vectors. This is the fast inner
loop; it catches essentially everything before Vitis is involved.

### Full hardware flow

```bash
vitis_hls -f hls/tcl/run_all.tcl               # csim, csynth, cosim, export IP
vivado -mode batch -source vivado/build_vivado.tcl
xsct ps/build_ps.tcl
xsct ps/run_hw.tcl -record 100
python python/scripts/serial_rx.py --port COM5 --record 100
```

One micro-USB cable does all of it: `COM5` above is the Digilent COM port on the board's
PROG connector, which carries JTAG, the firmware log and the compressed frame. The
firmware keeps the console silent while the frame is going out so the two can share the
wire, and the receiver resynchronises on the frame's sync word.

See [docs/hardware_bringup.md](docs/hardware_bringup.md) for prerequisites, pass
criteria and a fault-isolation guide.

### Reports

```bash
python python/scripts/resource_budget.py --report resource_budget.md
python python/scripts/verify_matrix.py                       # all records, model only
python python/scripts/verify_matrix.py --mode hardware --port COM5
```

## Design decisions worth knowing about

**One global threshold set, never per-record.** `BAND_THRESHOLD` in `config.py` was
chosen by sweeping the whole 46-record population and taking the most aggressive set
that still meets both targets everywhere. Record 213 is the binding case at CR 5.29.
Tuning per record would produce better-looking numbers and a system that cannot be
deployed.

**Two PRD conventions are reported.** The spec evaluates against "raw signals", which is
the baseline-inclusive reference used throughout the compression literature and the
convention the "PRD < 2% with CR > 5" pairing comes from; that is what the gate uses
(`config.PRD_GATE_METRIC`). The strict zero-mean PRD is always printed alongside it and
is much larger, because the between-beat baseline is where the bits are being saved. The
QRS figure is 0% under either convention.

**No multipliers except the energy operator.** The IIR bandpass uses shift-based
exponential averages and the Haar transform uses a reversible lifting scheme, so both
are adds and shifts. The lifting scheme is also exactly invertible, so the transform
itself contributes no distortion at all: all of the loss is in the thresholding, where
it is controlled.

**Records are swapped without re-synthesising.** The sample store is a true dual-port
block RAM. The core reads one port; the other is on an AXI BRAM controller, so a new
record is written straight into memory over JTAG or by the PS.

## Requirements

Python 3.11+, and for the hardware flow Vitis HLS / Vivado / Vitis 2023.2 with the
Digilent board files installed.
