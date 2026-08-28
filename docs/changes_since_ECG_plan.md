# Changes and Fixes Since `docs/ECG_plan.md`

This document records what diverged from, or was added after, the execution plan in
[`ECG_plan.md`](ECG_plan.md). The plan generated the repo layout and phased build; the
items below are the implementation and board-bring-up deltas that were required to make
the Zybo Z7-10 (XC7Z010) design build, fit, and be testable with one cable.

**Programming and communication cable:** the board’s **PROG micro-USB is the only cable
used** for JTAG programming, the PS console, and compressed-frame capture. Do **not**
use a second USB-UART adapter or Pmod JE1 for data; the current firmware sends the frame
on the PS UART over that same micro-USB Digilent COM port.

Status snapshot: Python golden model and native HLS bit-exactness were closed early.
Vivado place/route and PS UART bring-up required several rounds of fixes after the
planned files existed.

**Device slice budget (Place context):** the XC7Z010 has **4400 slices** total. In the
failing place reports that motivated the area work, only about **2647 slices were
available** for packing (the rest already consumed / constrained), and each slice can
hold only one unique control set — so LUT count alone understates how tight packing was.

---

## 1. Plan divergences during implementation (not “bugs”, architecture choices)

These are intentional departures from the plan’s first wording, made while generating
the code so the design could fit the device and stay bit-exact.

| Plan said | What shipped | Why |
|---|---|---|
| Phase 4: GUI BD then `write_bd_tcl` | Hand-authored [`vivado/create_bd.tcl`](../vivado/create_bd.tcl) + headless [`build_vivado.tcl`](../vivado/build_vivado.tcl) | Reproducible scripted flow; addresses locked to match [`ps/src/ecg_hw.h`](../ps/src/ecg_hw.h) |
| Pure DATAFLOW producer/consumer for encode+pack | Detect then encode in one process; header built on the **PS** after status is known | Streaming master cannot rewrite a checksum header already sent; sample BRAM already uses most on-chip memory |
| Sample store as 16-bit port B asymmetric BRAM | **32-bit** true dual-port BRAM; two int16 samples per word (low = sample 2i) | Vivado rejected asymmetric 32/16 TDP for this flow; packing matches `.coe` / `100.bin` |
| Lossy segment = whole non-QRS run | Cap at `ECG_MAX_LOSSY_SEGMENT` with multiple lossy packets | Full-record buffers do not fit remaining BRAM |
| Frame out PL UARTLite on Pmod JE1 | Frame out **PS UART** (PROG micro-USB); UARTLite left in BD unused | One-cable testing; see §5 |
| `ps/build_ps.tcl` domain `standalone_ps7_cortexa9_0` | Real platforms often use `standalone_domain` | Tool/version naming; see §6 |

Also added beyond the plan skeleton:

- `hls/build_native.py` — bit-exact g++ path without Vitis
- `python/scripts/calibrate_thresholds.py`, `verify_matrix.py`, `resource_budget.py`, `export_hls_constants.py`
- `docs/hardware_bringup.md`, `toolchain_gates.md`, `verification_matrix.md`, `resource_budget.md`
- `testing/` kit (ELF/BIT/XSA handoff + host scripts + bring-up steps)

---

## 2. HLS / Vitis toolchain fixes

### 2.1 Tcl `::argv` treated `-f` as the MIT-BIH record id

**Symptom:** `vitis_hls -f hls/tcl/run_all.tcl` looked for golden data under `data/golden/-f/`.

**Cause:** When HLS launches with `-f <script>`, Tcl’s `::argv` still contains the tool’s
own `-f` and script path. Blindly taking `argv[0]` as the record id was wrong.

**Fix:** [`hls/tcl/common.tcl`](../hls/tcl/common.tcl) only accepts a non-flag, non-`.tcl`
token (the `-tclargs` value) as `ECG_RECORD`.

### 2.2 Packer / AXIS / TLAST

**Issues hit during module synth and top integration:**

- Packer needed a one-word delay so **TLAST** marks the true last AXI word without
  buffering the whole payload.
- Stream type moved to a proper AXI side-channel (`ap_axiu` / `hls::axis`) so TLAST/TKEEP
  are real; a plain C++ struct did not synthesise to TLAST and DMA could drop bytes.

**Verification:** asserted in `tb_top` / packer testbenches; native build + csim gates.

### 2.3 Why csim can pass but cosim fails (BRAM `depth`)

**Symptom:** C-simulation is bit-exact; RTL cosim segfaults (`COSIM 212-379`), hangs, or
sees garbage peaks / massive coefficient saturation / a huge wrong bitstream.

**Why they differ:**

- **C-sim** calls `ecg_top` with your **real** testbench array pointer. The whole packed
  sample store is whatever the TB allocated and filled.
- **Cosim’s wrapc adapter** allocates its **own** BRAM model for pointer/`bram` ports.
  For those ports HLS requires an **explicit `depth`**. Without it, the stub is too
  small: only a few samples are valid and the rest read as garbage.

**Fix:**

- `#pragma HLS INTERFACE bram port = ecg_bram depth = ECG_N_BRAM_WORDS` on `ecg_top`
- [`hls/tb/tb_util.h`](../hls/tb/tb_util.h) `pack_bram()` always pads the TB buffer to
  full `ECG_N_BRAM_WORDS` (wrapc copies that full depth; a short excerpt overruns the heap)

### 2.4 Token FIFO / DATAFLOW BRAM overrun → Option A (depth 128) → inline pack

**Symptom:** Vivado DRC / BRAM over-budget (~82 BRAM36 vs 60 on the Z7-10). A sequential
encode-then-pack path used a **16K-deep BRAM** token FIFO (~24 BRAM36 alone) after an
earlier DATAFLOW+AXIS cosim deadlock.

**Fix (Option A, as chosen):** keep a **small** coder→packer stream,
`depth = 128` matching `TOKEN_FIFO_DEPTH` in [`python/ecg_ref/config.py`](../python/ecg_ref/config.py)
(`ECG_TOKEN_FIFO_DEPTH` in `ecg_constants.h`), bound to **SRL** (not BRAM), so the packer
can drain during encode without spending block RAM.

**Later evolution (what ships in `ecg_top` today):** concurrent DATAFLOW + AXIS still
deadlocked in RTL cosim (packer blocked on `TREADY`, TB waiting on `ap_done`). The top
now **packs inline** in one process (no on-chip token FIFO between encode and AXIS). The
`TOKEN_FIFO_DEPTH = 128` constant remains for module testbenches / packer-stage paths
that still use the token protocol. Comments in `ecg_top.cpp`, `bitpack.h`, and
`top_integrate.tcl` record this history.

### 2.5 Floor-division / fixed-point helpers

Python `//` is floor division; C `/` truncates toward zero. Helpers such as
`ecg_floor_div` (and related mulhi / ASR helpers in `ecg_types.h`) were added so HLS
matches `fixedpoint.py` on negative numerators and large products.

### 2.6 Detection stream alignment

Early streaming TKEO was one sample late vs Python (needs `bp[n+1]`). Detection was
restructured so bandpass / TKEO / threshold indices match the reference exactly
(including boundary rules), restoring bit-exact peak lists.

---

## 3. FPGA resource / place-and-route fixes (`Place 30-487`)

**Symptom:** Implementation failed packing on XC7Z010 — combined LUTs above ~17 600,
high control-set count, FFs unable to pack one control set per slice. Device has
**4400 slices**; the failing runs had on the order of **2647 slices available** for the
remaining logic, so packing (not only raw LUT count) was the limiter.

### 3.1 Failed approaches (do not revive)

| Attempt | Result |
|---|---|
| `#pragma HLS ALLOCATION` on `ecg_bitpack_put` / emit from `ecg_top` | Pragmas were **ignored** (calls only exist inside child functions) and **crashed the compiler** (**HLS 200-1715** clang crash in Vitis HLS 2023.2). **All `ALLOCATION` pragmas were removed.** |
| Inlining QRS/non-QRS at **two** textual sites (gap + trailing) | Duplicated non-QRS pipelines → csynth ~17 630 LUTs (100% of device) |

### 3.2 Fixes that landed

| Change | File(s) | Effect |
|---|---|---|
| Removed all `ALLOCATION` pragmas | `ecg_top.cpp` (and related) | Stops ignored pragmas / HLS 200-1715 crash |
| Forced `#pragma HLS INLINE` on `ecg_encode_qrs` / `ecg_encode_nonqrs` | `lossless.cpp`, `lossy.cpp` | Both share **one** emit→bitpack body inside `encode_stage` |
| Single textual call site each for QRS / non-QRS (cursor + `next_start`/`next_end`) | `hls/src/ecg_top.cpp` | Prevents duplicated lossy pipelines |
| Kept emit/bitpack as `#pragma HLS INLINE off` | `bitpack.cpp` | One shared packer body (not duplicated per caller) |
| Kept `peaks[]` on BRAM (`ram_1p impl = bram`, not LUTRAM) | `ecg_top.cpp` | Saves ~750 LUTs |
| Kept `config_rtl -reset control` | `hls/tcl/common.tcl` | Fewer datapath reset nets / control sets |
| Detect pipeline `II = 2` (was 1) | `detect.cpp` | Free area; realtime headroom still huge |
| Synth `Flow_AreaOptimized_high`; impl `Area_Explore`; OPT `ExploreArea` | `vivado/build_vivado.tcl` | Better LUT/slice packing |

**Outcome:** Implementation completes; bitstream / XSA export usable for board test.
Temporary NDJSON instrumentation under `#region agent log` was used while debugging;
cleanup was requested after success — residual logging may still exist in
`hls/tcl/common.tcl` and `vivado/build_vivado.tcl` writing `debug-0362fe.log` (harmless
but not part of the functional design).

---

## 4. Sample-store / Vivado BD fixes

| Issue | Fix |
|---|---|
| Asymmetric BRAM port widths rejected / mismatched | Both ports 32-bit; HLS reads via packed half-words |
| Address map must match firmware | Fixed offsets in `create_bd.tcl`: BRAM `0x40000000`, DMA `0x40400000`, UARTLite `0x42C00000`, `ecg_top` `0x43C00000` |
| Record swap without rebuild | AXI BRAM controller port A + JTAG/`mwr` of `data/coe/<rec>.bin` |

UARTLite on JE1 (`tx_line` / V12) remains in the BD and bitstream for compatibility;
firmware no longer drives it after the PS-UART change (§5).

---

## 5. PS UART compressed-frame dump (micro-USB only)

**Plan / Phase 5 original:** DMA buffer → PL UARTLite → Pmod JE1 + USB-UART adapter;
PS console on micro-USB separate.

**Change:** Frame TX via BSP `outbyte()` on the **PS UART**.

**Cable rule (explicit):** the **PROG micro-USB is the only programming and
communication cable**. It carries:

1. JTAG (program `.bit` / download `.elf`)
2. PS console text (`xil_printf`)
3. The compressed binary frame (`serial_rx.py --port <Digilent COM>`)

No second adapter and no Pmod JE1 wiring are required or supported by the current
firmware path. Leave serial terminals closed on that COM while `serial_rx.py` runs.

### Firmware ([`ps/src/main.c`](../ps/src/main.c))

- Removed `xuartlite.h`, `XUartLite` instance, and `XUartLite_Initialize`
- `uart_send_blocking` = loop of `outbyte()` (not `printf` — avoids CRLF mangling of `0x0A`)
- **Quiet window:** all `xil_printf` status lines *before* sync word; no console I/O until
  payload ends; then `\r\ndone\r\n` / `FAILED`
- Banner kept at startup so testers can confirm the ELF is alive
- [`ps/src/ecg_hw.h`](../ps/src/ecg_hw.h): comment that `ECG_UART_DEVICE_ID` is unused by
  the frame path

### Host

- Wire format unchanged — [`python/scripts/serial_rx.py`](../python/scripts/serial_rx.py)
  still resyncs on `0x45434731` (`ECG1`) and ignores banner / trailing text
- Docs / kit point `--port` at Digilent PROG COM, not JE1 adapter
- Rule: only one process may own that COM (`serial_rx` vs PuTTY)

### Rebuild impact

- **ELF rebuild only** (`xsct ps/build_ps.tcl` or Vitis app build) — **no new bitstream**
  required for this strategy
- Stale ELF still TX on JE1 → PROG COM shows banner then nothing

### Docs updated

- [`docs/hardware_bringup.md`](hardware_bringup.md)
- [`testing/testing steps.md`](../testing/testing%20steps.md), `testing/hardware_bringup.md`
- [`README.md`](../README.md) bring-up blurb
- `ps/run_hw.tcl` / `testing/run_hw.tcl` capture hint

---

## 6. Board / XSCT / XSDB operational fixes (no RTL change)

These are procedure and environment fixes discovered while programming the Zybo.

| Error / symptom | Root cause | Fix |
|---|---|---|
| Domain `standalone_ps7_cortexa9_0` not valid | Platform names domain differently | Use `standalone_domain` (or omit `-domain`); prefer incremental `app build` over wiping workspace |
| `invalid command name ps7_init` | Procedures live in `ps7_init.tcl` | `source ps7_init.tcl` before `ps7_init` / `ps7_post_config` |
| No `APU*` / DAP AHB error | JTAG/PS not visible | Power, JP5=JTAG, PROG USB, close Vivado HW Manager / other XSDB |
| `loadhw` Invalid target / multi-match | Wrong node or ambiguous filter | `loadhw` on **APU**; `dow`/`con`/`mwr` on **Cortex-A9 #0**; prefer `target_id` |
| `mwr` blocked at `0x40000000` (“PL AXI slave ports access is not allowed” / address not in memory map) | **JTAG doesn’t know this is a valid PL slave** until the debugger has a map and the PS–PL bridges are up | After `ps7_post_config`: `loadhw` (if HSI available) and/or `memmap -addr 0x40000000 -size 0x40000`; then `mwr`. Selecting APU for `loadhw` and Cortex-A9 #0 for `mwr` still applies |
| `invalid command name "::hsi::open_hw_design"` | XSDB session without HSI | Use full **XSCT** Vitis shell, or skip `loadhw` and use `memmap` |
| Stdout on wrong UART | BSP stdout bound to UARTLite | Banner on Digilent COM proves PS UART; if wrong: `domain config -stdout ps7_uart_1` |

Target roles (**same micro-USB cable**, three debug nodes):

1. **APU** — `loadhw` (address map)
2. **xc7z010** — `fpga -file …bit`
3. **Cortex-A9 #0** — `ps7_init`, `mwr`, `dow`, `con`

### 6.1 Blocked PL address in plain language

When XSDB says the write to `0x40000000` is blocked because the address has not been
added to the memory map, it means: **JTAG doesn’t yet treat that BRAM window as a valid
PL AXI slave** in its own map (and/or the PS–PL level shifters are still down). The
block design may already assign `0x40000000` in Vivado; that does not automatically make
XSDB allow `mwr`. Fix with `ps7_post_config` + `loadhw` and/or explicit `memmap`, then
retry the write.

---

## 7. Testing kit and bring-up documentation

| Addition | Purpose |
|---|---|
| `testing/` tree + `testing.zip` | Minimal handoff: bit, elf, xsa, `ps7_init.tcl`, host Python, steps |
| [`testing/testing steps.md`](../testing/testing%20steps.md) | End-to-end XSDB + `serial_rx` from built artifacts |
| Bring-up fault trees updated for PS-UART path | “No sync” / “checksum mismatch” now point at quiet-window / COM ownership |

Pass criteria for record 100 (unchanged from plan Phase 6):

- `vs golden BIT-IDENTICAL`
- checksum OK
- PRD QRS = 0%
- overall (baseline) PRD &lt; 2%
- CR &gt; 5

---

## 8. What still matches the plan

- Python reference is the bitstream source of truth; HLS gated bit-exact against golden
- Global band thresholds only (no per-record tuning); 46/46 MLII gate in verification matrix
- QRS lossless / lossy Haar+RLE / 32-bit packed frame / additive checksum
- Zybo Z7-10, ~100 MHz PL, AXI DMA S2MM into DDR, AXI-Lite status, A9 global timer latency
- Record swap via BRAM rewrite without re-synthesis

---

## 9. Suggested reading order for a new tester

1. This file — what changed and why  
2. [`hardware_bringup.md`](hardware_bringup.md) — rebuild ELF + capture on Digilent COM  
3. [`testing/testing steps.md`](../testing/testing%20steps.md) — program and run from kit files  
4. [`ECG_plan.md`](ECG_plan.md) — original phased intent (historical)

---

## 10. File index of post-plan hotspots

| Area | Primary files |
|---|---|
| Area / encode sharing | `hls/src/ecg_top.cpp`, `bitpack.cpp`, `lossless.cpp`, `lossy.cpp`, `detect.cpp` |
| Token FIFO / Option A / inline pack | `ecg_top.cpp`, `bitpack.h`, `config.py` (`TOKEN_FIFO_DEPTH=128`), `top_integrate.tcl` |
| HLS Tcl / reset / argv | `hls/tcl/common.tcl` |
| Vivado strategies / BD | `vivado/build_vivado.tcl`, `vivado/create_bd.tcl` |
| Cosim BRAM depth (csim vs cosim) | `hls/tb/tb_util.h`, `hls/src/ecg_top.cpp` |
| PS UART / micro-USB-only path | `ps/src/main.c`, `ps/src/ecg_hw.h` |
| Host / docs | `python/scripts/serial_rx.py`, `docs/hardware_bringup.md`, `testing/testing steps.md` |
