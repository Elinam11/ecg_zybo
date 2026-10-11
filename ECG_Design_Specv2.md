# Technical Design Specification: FPGA-Based Adaptive ECG Compression System

**Document:** Implementation-aligned revision (v2)  
**Base document:** `ECG_Design_Specification.md`  
**Target Platform:** Zybo Z7-10 (Xilinx Zynq-7010 XC7Z010-1CLG400C)  
**Design Tools:** AMD Vitis HLS 2023.2 / Vivado Design Suite (2023.2) / Python 3.10+

This revision preserves the original section numbering and headings so the two documents can be compared side by side. Text describes the behaviour that is actually implemented in the repository.

---

## 1. Executive Summary

This document provides the complete engineering design specification for a real-time, FPGA-accelerated adaptive ECG signal compression system targeting the Zybo Z7-10 development board (Xilinx Zynq-7010 XC7Z010-1CLG400C).

To guarantee clinical diagnostic integrity where waveform morphology is critical, the compression engine executes a hybrid dual-path compression scheme:

- **QRS Regions:** High clinical significance regions compressed strictly losslessly using a 2nd-order linear predictor paired with an adaptive Golomb-Rice entropy coder (with an escape path for pathological residuals).
- **Non-QRS Regions:** Baseline, P-wave, and T-wave intervals compressed lossily using a 4-level **reversible integer Haar (lifting / S-transform)** Discrete Wavelet Transform (DWT), band-dependent hard thresholding, and Zero-Run-Length Encoding (RLE).

Offline preprocessing (dataset extraction, lead selection, and 4th-order Butterworth bandpass filtering) is performed on a host laptop. The resulting preprocessed ECG record is loaded into on-chip Block RAM (BRAM) — either by bitstream `.coe` initialisation or by JTAG write of a packed `.bin` image at run time. The FPGA Programmable Logic (PL) performs all compression processing: a first-order shift-based IIR bandpass for detection, Teager-Kaiser Energy Operator (TKEO) calculation, dynamic energy threshold tracking with warmup and bidirectional peak search, fixed QRS window framing (up to 73 samples inclusive), dual-path compression, and bitstream packing.

The Zynq Processing System (PS) manages control, stages the compressed frame in **DDR via AXI DMA**, measures latency with the Cortex-A9 global timer, and streams the frame back to the host over the **PS UART** (PROG micro-USB) for reconstruction and performance evaluation. External DDR is used only as a staging buffer for the compressed bitstream; the sample store and intermediate compression state remain on-chip.

---



## 2. System Objectives & Design Constraints



### 2.1 System Objectives

- **Clinical Diagnostic Integrity:** Guaranteed zero distortion (PRD = 0%) across all QRS complex regions. Target overall signal Percentage Root Difference (PRD) < 2% on the **baseline-inclusive** PRD metric across all 46 MLII-bearing MIT-BIH records. A strict zero-mean PRD is always computed and reported alongside the gated figure.
- **High Compression Efficiency:** Achieve an overall Compression Ratio CR > 5:1 on 5-minute standard MIT-BIH Arrhythmia Database Lead MLII recordings (first 108,000 samples).
- **Deterministic Low Latency:** Measure end-to-end PL run latency with the PS Cortex-A9 global timer around `ap_start` … `ap_done`, reported in PL clock cycles (and microseconds) in the firmware banner and UART transport preamble.
- **Seamless Data Swapping & Reusable Verification:** Swap records by regenerating `data/coe/<id>.bin` / `*_x32.coe` and either rebuilding BRAM initialisation or writing the `.bin` into the sample store over JTAG — without changing HLS or RTL source. The Python reference model and HLS share constants exported from a single `config.py`.
- **Hardware Resource Optimization:** Fit the Vitis HLS IP, sample store, and coefficient buffers within the Zynq-7010 PL BRAM budget (60 × 36 Kb). The sample store dominates (~53 BRAM36). Compressed output is staged in PS DDR via DMA rather than buffered entirely in PL BRAM.



### 2.2 Hardware & Platform Constraints


| Constraint           | Value                                                                                                   |
| -------------------- | ------------------------------------------------------------------------------------------------------- |
| Target Device        | Xilinx Zynq-7010 (XC7Z010-1CLG400C) on Zybo Z7-10 board                                                 |
| Block RAM (BRAM)     | 60 x 36Kb blocks (~2.1 Mb total capacity) — hosts the 5-minute 16-bit preprocessed ECG vector (packed two samples per 32-bit word) |
| DSP Slices (DSP48E1) | 80 available slices                                                                                     |
| Logic Capacity       | 35,200 Flip-Flops (FF) | 17,600 Look-Up Tables (LUT)                                                    |
| Software Toolchain   | AMD Vitis HLS 2023.2, Vivado Design Suite 2023.2, Python 3.10+                                          |
| Usable MIT-BIH set   | 46 MLII-bearing records (all 48 arrhythmia records except 102 and 104, which have no MLII lead)         |


---



## 3. Hardware / Software Partitioning & Data Flow

The operational flow separates offline data preparation/evaluation on the host laptop from real-time core hardware execution within the Zynq-7010 SoC.

> **CRITICAL ARCHITECTURAL NOTE ON DATA FLOW — Data Ingest & Memory Architecture Clarification**
>
> When the system is running, there is **no streaming ECG input connection** between the host laptop and the FPGA. All ECG input data resides in on-chip Block RAM. AXI-Stream carries the compressed output from the HLS core to AXI DMA (S2MM), which writes the payload into PS DDR. The PS then transmits the transport-framed bytes over the PS UART. An `axi_uartlite` remains in the block design (Pmod JE1) but is unused by the firmware for frame transfer.



### 3.1 Functional Partitioning


| Subsystem        | Execution Environment            | Primary Responsibilities                                                                                                                                                                                 |
| ---------------- | -------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Preprocessing    | Host Laptop (Python 3.10+)       | Read MIT-BIH `.dat`/`.hea`, select MLII lead, apply 4th-order Butterworth bandpass (0.5–40 Hz, zero-phase), quantize to 16-bit signed integers, generate BRAM init files (`.coe`, `*_x32.coe`, `.bin`, `.dat`) |
| Real-Time Engine | Zybo Zynq-7010 PL (Vitis HLS IP) | Two-pass over BRAM: (1) detect R-peaks; (2) encode QRS/non-QRS segments and pack onto AXI4-Stream. Detection uses shift-based IIR (~7.6–39.7 Hz), TKEO, MA L=18, adaptive threshold, refractory 72, ± peak search, windows R−29…R+43 (≤73 samples) |
| Control & Ingest | Zynq PS (ARM Cortex-A9) & Laptop | PS arms DMA into DDR, starts the core, times the run, builds the 128-bit frame header from status registers, sends UART sync/length/latency + header + big-endian payload. Laptop `serial_rx` / `score_captures` decode and score CR/PRD vs golden |


---



## 4. Complete Processing Pipeline & Data Flow

The complete end-to-end processing pipeline operates across three distinct computational boundaries:

```
┌──────────────────────────────────────────────────────────────┐
│ HOST LAPTOP (OFFLINE)                                         │
│ 1. Read MIT-BIH (.dat/.hea) -> Extract Lead MLII              │
│ 2. Apply 4th-order Butterworth Bandpass Filter (0.5-40 Hz)    │
│ 3. Quantize to 16-bit signed integers ->                      │
│    Generate Memory Init (.coe / *_x32.coe / .bin / .dat)      │
└──────────────────────────────────────────────────────────────┘
                    │ (.coe at bitstream build, or JTAG .bin write)
                    v
┌──────────────────────────────────────────────────────────────┐
│ ZYBO ZYNQ-7010 FPGA (ON-BOARD)                                 │
│                                                                │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ On-Chip Block RAM (true dual-port, 32-bit words)      │    │
│  │ - Holds 5-minute preprocessed ECG record              │    │
│  │   (108,000 samples × 16 bits, two samples per word)   │    │
│  └──────────────────────────────────────────────────────┘    │
│                    │ Local BRAM (HLS port, latency=2)          │
│                    v                                            │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ Vitis HLS Core IP (ecg_top)                           │    │
│  │ Pass 1: Detect                                        │    │
│  │  1. Shift-based IIR bandpass (~7.6–39.7 Hz)           │    │
│  │  2. TKEO + Moving Average (L=18)                      │    │
│  │  3. Adaptive threshold + warmup + refractory          │    │
│  │  4. Bidirectional peak search → R indices             │    │
│  │ Pass 2: Encode + pack                                 │    │
│  │  5. QRS windows (R-29..R+43, clamped, ≤73)            │    │
│  │  6. Lossless: 2nd-order predictor + Golomb-Rice       │    │
│  │  7. Lossy: 4-level lifting Haar + threshold + RLE     │    │
│  │  8. Inline bit packer → AXI4-Stream (+ TLAST)         │    │
│  └──────────────────────────────────────────────────────┘    │
│                    │ Internal AXI4-Stream                      │
│                    v                                            │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ axi_dma S2MM → S_AXI_HP0 → DDR staging buffer         │    │
│  │ PS: build header, UART transport preamble + payload   │    │
│  └──────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────┘
                    │ [PS UART on PROG micro-USB, 115200 8N1]
                    v
┌──────────────────────────────────────────────────────────────┐
│ HOST LAPTOP                                                    │
│ 1. Resync on sync word; receive frame; optional --save         │
│ 2. Decode (Golomb-Rice + inverse Haar + RLE)                   │
│ 3. Score CR / PRD (baseline + strict) / vs golden              │
└──────────────────────────────────────────────────────────────┘
```

---



### Pipeline & Functional Module Specifications



### 4.1 Adaptive Detection & Segmentation Block

Detection runs as a full-record pass that produces R-peak indices; compression is a second pass. The record is random-access in BRAM, so a single streaming lookahead buffer is not required.

1. **Pre-Detection Bandpass Filter:** A resource-minimised first-order IIR bandpass built from two power-of-two exponential moving averages (no DSP multipliers):
   - LP shift 1 → α = 1/2 → fc ≈ 39.7 Hz  
   - HP shift 3 → α = 1/8 → fc ≈ 7.6 Hz  
   Effective passband ≈ **7.6–39.7 Hz** (intentionally different from the original 1.8–7.2 Hz nominal band). Accumulators carry 8 fractional bits.
2. **Teager-Kaiser Energy Operator (TKEO):** Computes instantaneous signal energy on filtered samples x[n]:
  $$\Psi[x(n)] = x^2(n) - x(n-1) \cdot x(n+1)$$
   End samples without a full neighbour pair are defined as zero.
3. **Moving Average Smoothing Filter:** Smooths the non-linear energy response using a rectangular window of length L = 0.05 × fs = **18** samples (at fs = 360 Hz), with floor division and zero-padded taps before record start.
4. **Adaptive Threshold Tracking:** Tracks baseline energy with a leaky accumulator:
  $$Threshold[n] = Threshold[n-1] + \frac{Smooth[n] - Threshold[n-1]}{128}$$
   (implemented as an arithmetic right shift by 7 on a fractional accumulator). Threshold initial value is 0.
5. **Warmup, Peak Detection & Refractory Counter:** Triggers are suppressed for the first **THRESH_WARMUP = 720** samples (2 s) while the threshold settles. An R-peak trigger is flagged when `Smooth[n] > 3 × Threshold[n]`. Upon detection, a refractory counter of **REFRACTORY = 72** samples (200 ms) suppresses secondary detections.
6. **Bidirectional Peak Search:** On a trigger, the R apex is the sample of largest absolute magnitude in `[trigger − 25, trigger + 25]` (clamped to the record), ties taking the earliest index. This corrects TKEO/MA group delay and leading-edge firings on quiet baselines.
7. **Window Framing & Stream Routing:** Each validated R-peak produces a QRS window:
  - Nominal: `Q_start = R − 29` (−80 ms), `S_end = R + 43` (+120 ms) → **73 samples inclusive**
  - Windows are clamped to the record and made non-overlapping by advancing `start` past the previous window’s end; truncated windows shorter than 73 are allowed and carry an explicit length field.
  - Non-QRS gaps between windows (and leading/trailing gaps) go to the lossy path. Gaps longer than **MAX_LOSSY_SEGMENT = 2048** samples are split into multiple packets (safety bound; real MIT-BIH gaps stay below this).



### 4.2 Lossless Compression Block (QRS Regions)

Applies to each QRS window (length L ≤ 73) to ensure exact waveform preservation:

1. **2nd-Order Linear Predictor:** Predicts sample ŷ(n) using previous two reconstructed samples:
  $$\hat{y}(n) = 2 \cdot y(n-1) - y(n-2)$$
2. **Prediction Residual Mapping (zigzag):** `e(n) = y(n) − ŷ(n)`, mapped to non-negative integers:
  $$M(e(n)) = \begin{cases} 2 \cdot e(n) & e(n) \ge 0 \\ -2 \cdot e(n) - 1 & e(n) < 0 \end{cases}$$
3. **Adaptive Golomb-Rice Encoding:** Over coded residuals (samples from index 2 onward; anchors y(0), y(1) sent verbatim), compute `S = Σ M(e)`. Optimal Rice parameter:
  $$k = \min\!\left(15,\ \max\!\left(0,\ \lfloor \log_2(S / N_{res}) \rfloor\right)\right)$$
   where \(N_{res}\) is the actual residual count (not a fixed 72/73), and `k` is transmitted in 4 bits.
4. **Escape coding:** If quotient `q = floor(M / 2^k) ≥ 31`, emit 31 ones (no terminator) followed by the raw 32-bit mapped residual, bounding pathological symbols.
5. Each normal symbol is unary `q` ones + terminating zero, then `k` remainder bits.



### 4.3 Lossy Compression Block (Non-QRS Regions)

Applies to baseline, P-wave, and T-wave intervals outside QRS windows:

1. **4-Level Discrete Haar Wavelet Transform (DWT):** Uses the **reversible integer lifting (S-transform)** form, not the original `floor(x/√2)` scaling:
  $$d = x_{even} - x_{odd},\qquad a = x_{odd} + \lfloor d/2 \rfloor$$
   The transform is exactly invertible (zero transform distortion). Segments are padded up to a multiple of **16** by **repeating the last sample**. Packet stores the true (unpadded) length.
2. **Band-Dependent Hard Thresholding:** Hard-threshold then pack surviving coefficients into a signed 10-bit field. Frozen global thresholds (one set for all records; Phase 1h calibration):

  | Band                       | Threshold      |
  | -------------------------- | -------------- |
  | Approximation Band (A4)    | T_A4 = **24**  |
  | Detail D4                  | T_D4 = **64**  |
  | Detail D3                  | T_D3 = **64**  |
  | Detail D2                  | T_D2 = **160** |
  | Detail D1                  | T_D1 = **160** |

   Per-band quantization shifts are currently **0** for all bands. Coefficients outside ±511 after scaling saturate and are counted in status.

3. **Zero-Run-Length Encoding (RLE):** Encodes into 16-bit tokens `[6-bit Zero Run Length (0–63) | 10-bit Quantized Signed Coefficient]`. Runs of length 63 emit a continuation marker `(63, 0)`. Trailing zeros are omitted; the decoder pads from the known coefficient count.  
   *(Note: the original “overflow at 255” wording does not apply; the run field is 6 bits, max 63.)*

---



## 5. Hardware Interface Definitions & Register Map



### 5.1 Top-Level FPGA IP Interfaces


| Port Name          | Direction    | Protocol / Type    | Width (bits) | Description                                           |
| ------------------ | ------------ | ------------------ | ------------ | ----------------------------------------------------- |
| `ap_clk`           | Input        | Clock              | 1            | 100 MHz PL main system clock                          |
| `ap_rst_n`         | Input        | Reset              | 1            | Active-low system reset                               |
| `s_axi_control` (`ctrl`) | Input/Output | AXI4-Lite Slave | 32      | `n_samples`, `ecg_status` (word_count, qrs_count, checksum, peak_count, saturated), ap_ctrl |
| `ecg_bram`         | Input        | BRAM (1-port)      | **32**       | Packed sample store (two int16 per word); HLS `latency=2`, `storage_type=ram_1p` |
| `out_stream`       | Output       | AXI4-Stream Master | 32           | Compressed payload words to DMA S2MM                  |
| `out_stream` TLAST | Output       | AXI4-Stream        | 1            | Asserted on the final payload word                    |


### 5.2 System Address Map (implemented)

| Peripheral / region        | Base address   | Notes |
| -------------------------- | -------------- | ----- |
| Sample store (AXI BRAM ctrl) | `0x40000000` | 32-bit addressing (`Enable_32bit_Address`); size `0x40000` |
| `ecg_top` AXI4-Lite        | `0x43C00000`   | Control / status |
| AXI DMA                    | BSP device ID  | S2MM into DDR |
| Frame staging buffer (DDR) | `0x10000000`   | 256 KB |
| `axi_uartlite`             | present in BD  | Unused by current firmware for frame I/O |



### 5.3 UART Transport Framing (PS → host)

Not part of the compressed frame itself. The PS prefixes each transmission with three big-endian words:

| Word | Content |
| ---- | ------- |
| 0    | Sync magic `0x45434731` (“ECG1”) |
| 1    | Frame length in 32-bit words (header + payload) |
| 2    | Measured latency in PL cycles |

Then: 4-word frame header + payload words, all **big-endian** on the wire. Banner/`xil_printf` text may precede the sync word; the host resynchronises byte-by-byte.

---



## 6. Metadata & Bitstream Format

The output bitstream generated by the FPGA engine consists of a fixed 128-bit Frame Header followed by a continuous sequence of tagged Segment Packets.

### 6.1 128-Bit Top-Level Frame Header Layout


| Word   | Bits     | Field                  | Description                                                                                 |
| ------ | -------- | ---------------------- | ------------------------------------------------------------------------------------------- |
| Word 0 | [31:0]   | Magic Word             | `0xEC601001`                                                                                |
| Word 1 | [63:32]  | Raw Sample Count       | Total raw 16-bit samples (e.g. 108,000)                                                     |
| Word 2 | [95:64]  | Sampling & Region Info | Bits [15:0] = Sampling Frequency (360) \| Bits [31:16] = QRS segment/packet count           |
| Word 3 | [127:96] | Verification Checksum  | 32-bit **additive** checksum over every payload word (init 0, mask `0xFFFFFFFF`)            |




### 6.2 Segment Packet Payload Syntax

Each segment begins with a 1-bit Header Tag:

- **QRS Segment Payload (Lossless):** Tag = **0** \| Length (16 bits) \| Anchor y(0) (16 bits) \| Anchor y(1) (16 bits) \| Rice k (4 bits) \| Golomb-Rice residual bits (with escape as in §4.2) \| Zero-padded to 32-bit boundary.
- **Non-QRS Segment Payload (Lossy):** Tag = **1** \| Length (16 bits) \| RLE symbol count (16 bits) \| Sequence of 16-bit RLE tokens \| Zero-padded to 32-bit boundary.

---



## 7. Performance Evaluation Metrics

Reconstructed waveforms on the laptop are evaluated against the **preprocessed int16** signal (the BRAM contents), not the raw ADC file:

1. **Compression Ratio (CR):**
  $$CR = \frac{N_{samples} \times 16}{B_{compressed}}$$
   Target: CR > 5. Header is included in the compressed bit count.
2. **Percentage Root Mean Square Difference (PRD):**
  - **Strict (zero-mean):** PRD against the bandpassed int16 reference as stored.  
  - **Baseline-inclusive:** Same error numerator, denominator uses reference + ADC mid-scale offset (1024).  
  **Gate:** baseline-inclusive PRD < 2%; QRS-region PRD exactly 0%; CR > 5; checksum OK.
3. **Processing Latency:**
  $$Latency\ (s) = \frac{N_{cycles}}{100\,\mathrm{MHz}}$$
   Measured by the PS around the core run (not a dedicated PL `LATENCY_CYCLES` register). Reported in the transport preamble and firmware log.

---



## 8. Vitis HLS Implementation & Testbench Architecture



### 8.1 Abbreviated Testbench Architecture

Per-module and top-level C++ testbenches live under `hls/tb/`. They load golden vectors and/or `.dat` / COE-derived stimuli from `data/golden` and `data/coe` (paths injected via environment from the HLS Tcl). Swapping the record id (Tcl `-tclargs <id>`) changes the stimulus without editing core sources. A native C++ gate (`hls/build_native.py`) also compares against the Python golden encoder.

### 8.2 Vitis HLS Core IP Implementation Strategy

The top-level `ecg_top` runs **two sequential passes** (detect, then encode/pack). An earlier DATAFLOW + deep token-FIFO architecture was abandoned: the sample store already consumes most BRAM, and DATAFLOW+AXIS deadlocked in RTL cosim. Packing is **inlined** into the encode stage onto the AXIS master.

Interfaces:

- `#pragma HLS INTERFACE bram` on `ecg_bram` (`depth` in 32-bit words, `ram_1p`, **latency = 2**)
- `#pragma HLS INTERFACE s_axilite` for `n_samples`, `status`, and return
- `#pragma HLS INTERFACE axis` for `out_stream`

BRAM loads of packed words use a non-inlined helper so the 2-cycle memory latency is honoured. QRS windows are copied through one sequential port into a local buffer before Rice coding (avoids requiring a second BRAM port that the block design does not wire).

---



## 9. Python Reference Implementation & Verification Strategy

The Python reference environment (`python/ecg_ref/`) is both the offline preprocessing pipeline and the gold-standard encoder/decoder:

1. **Off-Line Preprocessing:** Parses MIT-BIH, selects MLII, 4th-order Butterworth 0.5–40 Hz (`sosfiltfilt`), quantizes to int16 with gain normalisation to 200 ADU/mV, exports `.coe` / `*_x32.coe` / `.bin` / `.dat`.
2. **Frozen Band Thresholding:** Deployed path is band-dependent hard thresholding with the frozen set in `config.BAND_THRESHOLD`. Offline calibration (`calibrate_thresholds.py`) sweeps the three knobs (A4 / mid-detail / high-detail) globally; there is **no** runtime selection among unrelated algorithms (global-fixed or energy-proportional engines are not shipped as alternate compressors).
3. **Serial Ingest & Signal Reconstruction:** `testing/serial_rx.py` (and `python/scripts/serial_rx.py`) monitors the PROG COM port, resynchronises on the sync word, decodes the frame, scores CR/PRD, and compares byte-identity to `pipeline.encode_record`. Batch scoring of saved captures: `testing/score_captures.py`. Automated multi-record capture: `testing/capture_all.py` + `testing/run_record.tcl`.

---



## 10. Verification & Testbench Architecture

Dataset swap for HLS/csim uses COE/golden paths and a record id argument; core sources stay unchanged.

### 10.1 Comprehensive Verification Strategy

Python unit tests under `python/tests/` and HLS module/top testbenches enforce module-level and end-to-end checks. Reference verification matrix over **46** MLII records is produced by `verify_matrix.py --mode reference` (and scored hardware captures via `score_captures.py`).

1. **Module 1 (Detection & Windowing):**
  - R-peak scoring against `.atr` annotations within ±27 samples (±75 ms, AAMI EC57 style).
  - Non-overlapping window clamping so consecutive 73-sample windows cannot collide; shortened windows allowed.
2. **Module 2 (Lossless QRS Engine):**
  - 2nd-order residual + zigzag mapping; adaptive `k` over actual residual count; escape path.
  - Asserts **PRD = 0.0%** on QRS samples after decode.
3. **Module 3 (Lossy Non-QRS Engine):**
  - 4-level lifting Haar exact reversibility (unthresholded).
  - Band threshold application and 10-bit saturation accounting.
  - **RLE continuation:** long zero runs (> 63) emit `(63, 0)` continuation tokens (tested including lengths such as 255, 256, 1000 — not a 255-saturation overflow).
4. **Module 4 (Bitstream Packer & AXI4-Stream):**
  - 32-bit alignment, frame header, additive checksum, TLAST on stream end; transport loopback tests on the host.

### 10.2 Hardware Bring-Up Notes (implemented practice)

- JP5 = JTAG; single PROG USB for JTAG + PS UART @ 115200.
- Order: `loadhw` (APU) → `fpga` (fabric) → Cortex-A9 #0: `stop` / `rst -processor` / `ps7_init` / `ps7_post_config` / `memmap` → optional `mwr -bin` of `data/coe/<id>.bin` → `dow` ELF → **start host listener** → `con`.
- Strongest board gate: received frame **bit-identical** to Python golden for that record.

---



## 11. Constants Single Source of Truth *(new)*

All shared numeric parameters live in `python/ecg_ref/config.py`. `python/scripts/export_hls_constants.py` regenerates `hls/src/ecg_constants.h`. The PS includes that header so firmware, HLS, and Python cannot silently diverge on magic words, baud rate, sample count, or Rice/RLE field widths.

---



## 12. Change Log Relative to Original Spec *(new)*

Substantive differences from `ECG_Design_Specification.md` reflected above:

| Topic | Original | Implemented |
| ----- | -------- | ----------- |
| Record count | 45 | **46** MLII (exclude 102, 104) |
| Detection IIR band | 1.8–7.2 Hz | ~**7.6–39.7 Hz** shift-EMA |
| QRS window length | 72 samples | **73** inclusive (R−29…R+43); truncatable |
| Peak localisation | threshold crossing only | + **bidirectional** search ±25 |
| Threshold startup | — | **720-sample** warmup |
| Haar DWT | floor(x/√2) | **lifting / S-transform** |
| Band thresholds | 80 / 120 / 200 | **24 / 64 / 64 / 160 / 160** |
| Rice `k` divisor | fixed 72 | actual residual count; k≤15; **escape @ q≥31** |
| RLE overflow | “(255, 0)” | continuation at **63** |
| DDR use | “no external DDR” | **DMA → DDR** staging for compressed frame |
| UART | unspecified / Pmod path historically | **PS UART** + transport preamble |
| BRAM word width | 16-bit port wording | **32-bit packed**, 32-bit addressing |
| HLS structure | DATAFLOW stream pipeline | **two-pass** detect then encode; inline pack |
| Latency | PL LATENCY_CYCLES register | **PS global timer** + UART preamble field |
| PRD gate | ambiguous “raw” | **baseline-inclusive** < 2%; strict also reported |
| Wavelet engines | multiple algorithms | **one frozen** hard-threshold set (+ offline calibrator) |

Items tagged during review as not present / not verified in code:

- Host-side **waveform plotting** described in the original §9: no plotting dependency found in the Python tree. Reconstruction metrics are printed/CSV’d; plotting is out of scope of the current scripts.  
- Config comment mentioning raw short segments with symbol count 0: short non-QRS segments are **padded and DWT-coded**, not a separate raw-sample packet mode.

---

*End of implementation-aligned specification v2.*
