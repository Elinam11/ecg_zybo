# Technical Design Specification: FPGA-Based Adaptive ECG Compression System

**Target Platform:** Zybo Z7-10 (Xilinx Zynq-7010 XC7Z010-1CLG400C)

**Design Tools:** AMD Vitis HLS 2023.2 / Vivado Design Suite (2023.2) / Python 3.10+

---

## 1. Executive Summary

This document provides the complete engineering design specification for a real-time, FPGA-accelerated adaptive ECG signal compression system targeting the Zybo Z7-10 development board (Xilinx Zynq-7010 XC7Z010-1CLG400C).

To guarantee clinical diagnostic integrity where waveform morphology is critical, the compression engine executes a hybrid dual-path compression scheme:

- **QRS Regions:** High clinical significance regions compressed strictly losslessly using a 2nd-order linear predictor paired with an adaptive Golomb-Rice entropy coder.
- **Non-QRS Regions:** Baseline, P-wave, and T-wave intervals compressed lossily using a 4-level discrete Haar Wavelet Transform (DWT), band-dependent hard thresholding, and Zero-Run-Length Encoding (RLE).

Offline preprocessing (dataset extraction, lead selection, and 4th-order Butterworth bandpass filtering) is performed on a host laptop. The resulting preprocessed ECG record is pre-loaded into the Zynq-7010 Block RAM (BRAM) prior to execution. The FPGA Programmable Logic (PL) performs all real-time processing, including primary QRS bandpass filtering, Teager-Kaiser Energy Operator (TKEO) calculation, dynamic energy threshold tracking, fixed QRS window framing, dual-path compression, and bitstream packing.

The Zynq Processing System (PS) manages control and streams the generated output bitstream back to the host laptop over UART for reconstruction and performance evaluation. No external DDR is accessed in this system.

---



## 2. System Objectives & Design Constraints



### 2.1 System Objectives

- **Clinical Diagnostic Integrity:** Guaranteed zero distortion (PRD = 0%) across all QRS complex regions. Target overall signal Percentage Root Difference (PRD) < 2% across standard records.
- **High Compression Efficiency:** Achieve an overall Compression Ratio CR > 5:1 on 5-minute standard MIT-BIH Arrhythmia Database Lead MLII recordings.
- **Deterministic Low Latency:** Track hardware execution latency deterministically on a sample-by-sample basis using hardware timers controlled by the PS.
- **Seamless Data Swapping & Reusable Verification:** Provide a modular file-wrapper architecture in Vitis HLS and Vivado allowing any of the 45 MIT-BIH dataset records (.coe, .hex, or header-based stimulus) to be swapped cleanly without re-synthesizing top-level code.
- **Hardware Resource Optimization:** Fit the entire Vitis HLS IP hardware footprint, internal streaming FIFOs, and coefficient buffer structures strictly within the 270 KB (2.1 Mb / 60 Block RAM blocks) on-chip limit of the Zynq-7010 PL without utilizing external DDR memory for intermediate IP storage.



### 2.2 Hardware & Platform Constraints


| Constraint           | Value                                                                                                   |
| -------------------- | ------------------------------------------------------------------------------------------------------- |
| Target Device        | Xilinx Zynq-7010 (XC7Z010-1CLG400C) on Zybo Z7-10 board                                                 |
| Block RAM (BRAM)     | 60 x 36Kb blocks (~2.1 Mb total capacity) — sufficient to host 5-minute 16-bit preprocessed ECG vectors |
| DSP Slices (DSP48E1) | 80 available slices                                                                                     |
| Logic Capacity       | 35,200 Flip-Flops (FF) | 17,600 Look-Up Tables (LUT)                                                    |
| Software Toolchain   | AMD Vitis HLS 2023.2, Vivado Design Suite 2023.2, Python 3.10+                                          |


---



## 3. Hardware / Software Partitioning & Data Flow

The operational flow separates offline data preparation/evaluation on the host laptop from real-time core hardware execution within the Zynq-7010 SoC.

> **CRITICAL ARCHITECTURAL NOTE ON DATA FLOW — Data Ingest & Memory Architecture Clarification**
>
> When the system is running in real time, there is **no streaming ECG input connection active** between the host laptop and the FPGA. All ECG input data resides directly inside the FPGA's local Block RAM (BRAM). AXI-Stream interfaces are used internally within the FPGA logic to connect the compression IP output to the PS DMA/FIFO for UART transfer.



### 3.1 Functional Partitioning


| Subsystem        | Execution Environment            | Primary Responsibilities                                                                                                                                                                                 |
| ---------------- | -------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Preprocessing    | Host Laptop (Python 3.10+)       | Read MIT-BIH .dat/.hea files, select MLII lead, apply 4th-order Butterworth bandpass filter (0.5–40 Hz), quantize to 16-bit signed fixed-point integers, generate BRAM initialization file (.coe/.dat)   |
| Real-Time Engine | Zybo Zynq-7010 PL (Vitis HLS IP) | Fetch BRAM samples, apply 1.8–7.2 Hz IIR pre-filter, compute TKEO energy, smooth energy, track dynamic threshold, extract fixed QRS windows (R-29 to R+43), execute lossy/lossless paths, pack bitstream |
| Control & Ingest | Zynq PS (ARM Cortex-A9) & Laptop | PS handles AXI4-Lite control, measures cycle latency, receives AXI4-Stream bitstream, sends via UART. Laptop receives UART bytes, executes Python bitstream reconstruction, computes CR and PRD          |


---



## 4. Complete Processing Pipeline & Data Flow

The complete end-to-end processing pipeline operates across three distinct computational boundaries:

```
┌──────────────────────────────────────────────────────────────┐
│ HOST LAPTOP (OFFLINE)                                         │
│ 1. Read MIT-BIH (.dat/.hea) -> Extract Lead MLII              │
│ 2. Apply 4th-order Butterworth Bandpass Filter (0.5-40 Hz)    │
│ 3. Quantize to 16-bit signed integers ->                      │
│    Generate Memory Initialization (.coe)                      │
└──────────────────────────────────────────────────────────────┘
                    │ (Flashing / Synthesis Time)
                    v
┌──────────────────────────────────────────────────────────────┐
│ ZYBO ZYNQ-7010 FPGA (ON-BOARD)                                 │
│                                                                │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ On-Chip Block RAM (2.1 Mb total)                      │    │
│  │ - Holds 5-minute preprocessed ECG record              │    │
│  │   (~108,000 samples x 16 bits)                        │    │
│  └──────────────────────────────────────────────────────┘    │
│                    │ Local BRAM Memory Bus                     │
│                    v                                            │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ Vitis HLS Core IP                                     │    │
│  │ 1. Pre-detection Bandpass IIR Filter (1.8-7.2 Hz)     │    │
│  │ 2. TKEO Operator + Moving Average Filter (L=18)       │    │
│  │ 3. Adaptive Thresholding + Refractory Period Logic    │    │
│  │ 4. Adaptive Stream Router (Fixed QRS: R-29 to R+43)   │    │
│  │ 5. Lossless Path: 2nd-Order Predictor + Adaptive      │    │
│  │    Golomb-Rice Encoder                                │    │
│  │ 6. Lossy Path: 4-Level Haar DWT + Thresholding +      │    │
│  │    Run-Length Encoder                                 │    │
│  │ 7. Bitstream Stream Packer & AXI4-Stream Output       │    │
│  └──────────────────────────────────────────────────────┘    │
│                    │ Internal AXI4-Stream                      │
│                    v                                            │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ Processing System (PS - Dual ARM Cortex-A9)           │    │
│  │ - AXI DMA / FIFO Ingest -> UART Controller Driver     │    │
│  └──────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────┘
                    │ [Physical UART Serial Line]
                    v
┌──────────────────────────────────────────────────────────────┐
│ HOST LAPTOP (OFFLINE)                                          │
│ 1. Receive Bitstream Payload via Serial Com Port               │
│ 2. Python Bitstream Decoder (Golomb-Rice Unpack +              │
│    Inverse Haar + RLE)                                          │
│ 3. Signal Reconstruction & Performance Evaluation (CR > 5, PRD) │
└──────────────────────────────────────────────────────────────┘
```

---



### Pipeline & Functional Module Specifications



### 4.1 Adaptive Detection & Segmentation Block

To isolate QRS complexes reliably, the FPGA performs localized digital filtering prior to energy operator calculation:

1. **Pre-Detection Bandpass Filter:** A 1st-order digital IIR bandpass filter with passband 1.8 Hz to 7.2 Hz isolates the frequency band containing peak QRS spectral power.
2. **Teager-Kaiser Energy Operator (TKEO):** Computes instantaneous signal energy on filtered samples x[n]:
  $$\Psi[x(n)] = x^2(n) - x(n-1) \cdot x(n+1)$$
3. **Moving Average Smoothing Filter:** Smooths the non-linear energy response using a rectangular window of length L = 0.05 × fs = 18 samples (at fs = 360 Hz):
  $$Smooth[n] = \frac{1}{L} \sum_{k=0}^{L-1} \Psi[x(n-k)]$$
4. **Adaptive Threshold Tracking:** Tracks baseline energy fluctuations using a slowly varying dynamic threshold equation:
  $$Threshold[n] = Threshold[n-1] + \frac{Smooth[n] - Threshold[n-1]}{128}$$
5. **Peak Detection & Refractory Counter:** An R-peak is flagged when `Smooth[n] > 3 × Threshold[n]`. Upon detection, a refractory counter of duration R_refract = 72 samples (200 ms) is activated to suppress false secondary detections.
6. **Window Framing & Stream Routing:** Each validated R-peak triggers a fixed QRS boundary window:
  - `Q_start = R - 29 samples (-80 ms)`
  - `S_end = R + 43 samples (+120 ms)`
   Every ECG sample is sequentially evaluated against the R-peak threshold. Non-triggering samples default to the Lossy path. Upon detecting a valid R-peak, the system locks a fixed 72-sample window around that peak and diverts those specific 72 samples to the Lossless path before returning to standard lossy evaluation.



### 4.2 Lossless Compression Block (QRS Regions)

Applies exclusively to the 72-sample QRS intervals to ensure exact waveform preservation:

1. **2nd-Order Linear Predictor:** Predicts sample ŷ(n) using previous two reconstructed samples:
  $$\hat{y}(n) = 2 \cdot y(n-1) - y(n-2)$$
2. **Prediction Residual Mapping:** Calculated as `e(n) = y(n) - ŷ(n)`. Residuals are mapped to non-negative unsigned integers M(e(n)):
  $$M(e(n)) = \begin{cases} 2 \cdot e(n) & e(n) \ge 0  -2 \cdot e(n) - 1 & e(n) < 0 \end{cases}$$
3. **Adaptive Golomb-Rice Encoding:** Over the 72-sample window, computes residual sum `S = Σ M(e(m))`. The optimal Rice parameter k is determined adaptively:
  $$k = \max(0, \lfloor \log_2(S / 72) \rfloor)$$
   **Pseudocode of adaptive k-parameter estimation algorithm:**
   Each mapped residual is split into quotient `q = floor(M(e(n)) / 2^k)` (unary encoded) and remainder `r = M(e(n)) mod 2^k` (k binary bits).



### 4.3 Lossy Compression Block (Non-QRS Regions)

Applies exclusively to baseline, P-wave, and T-wave intervals outside QRS windows:

1. **4-Level Discrete Haar Wavelet Transform (DWT):** Decomposes Non-QRS sub-segments into 1 approximation band (A4) and 4 detail bands (D4, D3, D2, D1) using integer-scaled arithmetic.
  For input signal x[n], the pair-wise approximation a1[m] and detail d1[m] coefficients are calculated as:
   $$a_1[m] = \left\lfloor \frac{x[2m] + x[2m+1]}{\sqrt{2}} \right\rfloor \qquad d_1[m] = \left\lfloor \frac{x[2m] - x[2m+1]}{\sqrt{2}} \right\rfloor$$
2. **Band-Dependent Hard Thresholding:** Applies hard thresholding to wavelet coefficients c(i):
  $$c_{thresh}(i) = \begin{cases} 0 & |c(i)| < T_{band}  c(i) & \text{otherwise} \end{cases}$$
   Default band threshold values:

  | Band                       | Threshold      |
  | -------------------------- | -------------- |
  | Approximation Band (A4)    | T_A4 = 80      |
  | Mid-Detail Bands (D4, D3)  | T_D_LOW = 120  |
  | High-Detail Bands (D2, D1) | T_D_HIGH = 200 |

3. **Zero-Run-Length Encoding (RLE):** Encodes run lengths of zero coefficients into 16-bit packed symbol structures: `[6-bit Zero Run Length (0-63) | 10-bit Quantized Signed Coefficient]`.

---



## 5. Hardware Interface Definitions & Register Map



### 5.1 Top-Level FPGA IP Interfaces


| Port Name          | Direction    | Protocol / Type    | Width (bits) | Description                                           |
| ------------------ | ------------ | ------------------ | ------------ | ----------------------------------------------------- |
| `ap_clk`           | Input        | Clock              | 1            | 100 MHz PL main system clock                          |
| `ap_rst_n`         | Input        | Reset              | 1            | Active-low asynchronous system reset                  |
| `s_axi_control`    | Input/Output | AXI4-Lite Slave    | 32           | PS control, configuration, and status registers       |
| `ecg_bram_port`    | Input        | BRAM Port A/B      | 16           | Direct memory access interface to local BRAM array    |
| `out_stream`       | Output       | AXI4-Stream Master | 32           | Compressed output bitstream payload to PS DMA/FIFO    |
| `out_stream_tlast` | Output       | AXI4-Stream Master | 1            | Asserted high on final 32-bit word of execution frame |


---



## 6. Metadata & Bitstream Format

The output bitstream generated by the FPGA engine consists of a fixed 128-bit Frame Header followed by a continuous sequence of tagged Segment Packets.

### 6.1 128-Bit Top-Level Frame Header Layout


| Word   | Bits     | Field                  | Description                                                                                 |
| ------ | -------- | ---------------------- | ------------------------------------------------------------------------------------------- |
| Word 0 | [31:0]   | Magic Word             | `0xEC601001` (identifies system & architecture version)                                     |
| Word 1 | [63:32]  | Raw Sample Count       | Total raw 16-bit samples in processing frame (e.g., 108,000 for 5-minute record)            |
| Word 2 | [95:64]  | Sampling & Region Info | Bits [15:0] = Sampling Frequency (360 Hz) | Bits [31:16] = Total detected QRS regions count |
| Word 3 | [127:96] | Verification Checksum  | 32-bit checksum / reserved for control verification                                         |




### 6.2 Segment Packet Payload Syntax

Each segment (QRS or Non-QRS) begins with a 1-bit Header Tag identifying the payload structure:

- **QRS Segment Payload (Lossless):** Header Bit = 0 | Length (16 bits) | First Anchor Sample y(0) (16 bits) | Second Anchor Sample y(1) (16 bits) | Optimal Rice Parameter k (4 bits) | Concatenated Golomb-Rice Residual Bits | Zero-Padded to 32-bit boundary.
- **Non-QRS Segment Payload (Lossy):** Header Bit = 1 | Length (16 bits) | Total RLE Symbol Pairs (16 bits) | Sequence of Packed RLE Tokens [6-bit Run | 10-bit Value] | Zero-Padded to 32-bit boundary.

---



## 7. Performance Evaluation Metrics

Reconstructed waveforms generated on the laptop are evaluated against raw signals using standard engineering metrics:

1. **Compression Ratio (CR):**
  $$CR = \frac{\text{Total Input Uncompressed Bits}}{\text{Total Output Bitstream Bits}} = \frac{N_{samples} \times 16 \text{ bits}}{B_{compressed}}$$
   Target Constraint: CR > 5:1 across 5-minute MIT-BIH Lead MLII records.
2. **Percentage Root Mean Square Difference (PRD):**
  $$PRD = \sqrt{\frac{\sum_{n=1}^{N} (x(n) - x_{rec}(n))^2}{\sum_{n=1}^{N} x^2(n)}} \times 100$$
   Target Constraint: PRD = 0% across all QRS regions; overall signal PRD < 2%.
3. **Processing Latency:**
  $$Latency \text{ (seconds)} = \frac{LATENCYCYCLES}{ClockFreq}$$
   Measured via PS hardware timers reading the LATENCY_CYCLES register.

---



## 8. Vitis HLS Implementation & Testbench Architecture



### 8.1 Abbreviated Testbench Architecture

The Vitis HLS testbench uses an isolated memory wrapper design. An independent C++ file reader class reads pre-filtered ECG sample data from a disk file into a simulated BRAM memory array before triggering core execution.

To swap input datasets, developers update only the target input data file path in the testbench configuration. The core HLS source code, synthesis directives, and testbench structures remain completely unchanged.

### 8.2 Vitis HLS Core IP Implementation Strategy

The top-level C++ module processes data using coarse-grained task-level pipelining enforced via `#pragma HLS DATAFLOW` directives. Communication between pipeline sub-functions (IIR pre-filter, TKEO operator, moving average, threshold tracker, and dual-path encoders) is implemented using `hls::stream<>` FIFOs.

Memory interfaces are bound to BRAM using `#pragma HLS INTERFACE bram`, control registers are mapped to AXI4-Lite using `#pragma HLS INTERFACE s_axilite`, and the output stream is exported via AXI4-Stream using `#pragma HLS INTERFACE axis`.

---



## 9. Python Reference Implementation & Verification Strategy

The Python reference environment acts as both the offline preprocessing pipeline and the gold-standard verification tool:

1. **Off-Line Preprocessing:** Parses MIT-BIH records (.dat/.hea), selects MLII lead, applies a 4th-order Butterworth Bandpass Filter (0.5–40 Hz), quantizes samples to 16-bit signed integers, and exports formatted `.coe` / `.dat` memory files for BRAM initialization.
2. **Flexible Wavelet Thresholding Engine:** Includes modular code supporting multiple thresholding algorithms (band-dependent hard thresholding, global fixed thresholding, and energy-proportional adaptive thresholding) to allow evaluation of lossy compression tradeoffs.
3. **Serial Ingest & Signal Reconstruction:** Monitors the laptop's serial COM port, receives raw compressed bytes streamed over UART from the Zynq PS, unpacks segment headers, restores Golomb-Rice and Haar-RLE payloads, calculates exact CR and PRD metrics, and plots original vs. reconstructed waveforms side-by-side.

---



## 10. Verification & Testbench Architecture

The Vitis HLS testbench uses an abstract COE file wrapper, allowing dataset updates without modifying source or testbench files.

### 10.1 Comprehensive Verification Strategy

To guarantee that **every individual module and interface** functions correctly in hardware synthesis and RTL co-simulation, the testbench enforces unit-level testing with detailed assertions alongside end-to-end verification across all 45 MIT-BIH records.

1. **Module 1 Verification (TKEO Router & Dynamic Windowing):**
  - Verifies R-peak detection accuracy against MIT-BIH annotation files (.atr).
  - Validates that QRS complex boundary pointers [Q_start, S_end] correctly prevent window truncation during rapid consecutive heartbeats.
2. **Module 2 Verification (Lossless QRS Engine):**
  - Confirms 2nd-order predictor residual calculation and ZigZag mapping.
  - Tests adaptive Rice parameter k calculations over sliding 72-sample windows to prevent integer underflow/overflow.
  - Asserts **zero distortion (PRD = 0.0%)** between raw input QRS samples and decoded output QRS samples.
3. **Module 3 Verification (Lossy Non-QRS Engine):**
  - Checks 4-level Haar Discrete Wavelet Transform coefficient accuracy.
  - Confirms threshold application across sub-bands.
  - **Overflow Tuple Emission Test:** Specifically streams long continuous sequences of zeros (> 255 samples) to verify that the RLE engine correctly forces an overflow tuple emission (255, 0) without counter saturation or data corruption.
4. **Module 4 Verification (Bitstream Packer & AXI4-Stream Interface):**
  - Validates byte alignment, 32-bit word boundary alignment, dynamic frame header insertion, and TLAST signal assertion on stream termination.

