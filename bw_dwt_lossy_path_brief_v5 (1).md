# Brief: Replace the Lossy Path (4-level HWT) with a Biorthogonal-Wavelet DWT + Dead-Zone Quantizer + RLE + Golomb-Rice Coder

**Your job:** first PLAN, then wait for approval, then implement. Do not start coding before the plan is approved.

Legend: **[FIXED]** = decided, do not change. **[DEFAULT]** = recommended, you may justify a change. **[DECIDE]** = open, evaluate options and recommend, check with experiments and measurements **[VERIFY]** = check with your own tests before relying on it.

### Notation: symbols that look alike (read before anything else)

Several symbols in this brief look alike but are unrelated. Keep them apart in prose, code and tests. Never write a bare `delta`, `K`, `k`, `a` or `M` in code; use the prefixed names below.

| Brief symbol | Meaning | Where | Do NOT confuse with | Use in code |
|---|---|---|---|---|
| `alpha, beta, gamma, delta` | the four **lifting** constants of the 9/7 transform | 5.1a | `a`, `Delta`, quantizer `delta` | `LIFT_ALPHA`, `LIFT_BETA`, `LIFT_GAMMA`, `LIFT_DELTA` |
| `K` (capital) | **lifting** scaling constant, 1.2301741... | 5.1a | `K_max`, Rice `k`, bin index `k` | `LIFT_K` |
| `a` | dead-zone **ratio**, `Delta = a * T`, 1.40 initially | 4, 5.3 | `alpha` | `DZ_A` |
| `T` | dead-zone **threshold** (global) | 4, 5.3, 5.4 | | `DZ_T` |
| `Delta` (capital) | quantizer **step size** `= a * T` | 5.3, 5.4 | lowercase `delta` | `QUANT_STEP` |
| `delta` (lowercase, **only inside the 5.4 equations**) | half step `= Delta/2`, a local symbol of the printed decision intervals | 5.4 | the lifting constant `delta` (0.4435) | `QUANT_HALF_STEP`, or just write `Delta/2` |
| `k` in `I_k`, `O_k` | quantizer **bin index** (`q` is the quantized value) | 5.4 | Rice `k` | `q` |
| `k`, `k_run`, `k_lvl` | **Rice** parameter (`m = 2^k`) | 5.5 | `K`, bin index | `RICE_K_RUN`, `RICE_K_LVL` |
| `K_max` | upper limit of the **Rice** parameter | 5.5 | `K` (lifting) | `RICE_K_MAX` |
| `m` (lowercase) | Rice divisor `2^k` | 5.5 | `M` (margin) | `rice_m` |
| `M` (capital) | boundary **margin** in samples, method (b) | 5.0 Step 4 | `m` | `BND_MARGIN` |
| `L`, `L_seg`, `L_max` | decomposition levels (global max / per segment) | 5.0 Step 3 | | |
| `Q_ESC`, `B` | Rice escape: unary threshold, raw bit width | 5.5 | | `RICE_Q_ESC`, `RICE_ESC_BITS` |

---

## 1. Goal and problem statement

- The existing lossy path uses a **4-level reversible Haar-type wavelet transform (HWT)** followed by the current quantization and entropy coding.
- **Problem: the strict PRD of the lossy path is too high.**
  - Target: strict PRD <= 2 at CR >= 5.
- **Approach:** replace the lossy path's transform, quantizer and entropy coder with a paper-derived scheme:
  1. Biorthogonal wavelet DWT (bior4.4 family)
  2. Dead-zone quantization (DZQ) with an energy-based threshold
  3. Zero-run RLE
  4. Zigzag mapping + Golomb-Rice coding, parameter chosen **per subband**

## 1a. the lossy path works on segments routed to it, not the whole record

This is an **adaptive** system. Pass 1 (detection) finds QRS regions; **non-QRS regions are sent to the lossy compressor** (QRS regions go to the lossless path). Everything below that mentions "the signal", "the record"  in a transform context must be read as **"one lossy segment"**.   (Section 5.0):

1. Segments are short and of variable length. A 5-level, 9/7-tap transform has an effective support at level 5 of roughly 250 samples, which may be as long as the whole segment. 
2. Boundary handling is no longer a minor detail. Every segment has two boundaries, and both sit next to a steep QRS flank.
3. Per-segment side information and entropy-coder warm-up become a real overhead (Section 5.5, 5.7).


---

## 2. Scope: what changes and what does not

| Component | Status |
|---|---|
| Offline host preprocessing (Section 3) | **[FIXED] unchanged** |
| Detection pass (pass 1) | **[FIXED] unchanged** |
| Lossless path | **[FIXED] unchanged** |
| Packet / bitstream format | **[FIXED] unchanged**; the new payload must fit it (see 5.7) |
| Two-pass PL structure (detect, then compress/encode/pack) | **[FIXED] unchanged** |
| 4-level HWT (lossy) | **REPLACED** by the DWT in 5.1 |
| Lossy quantization | **REPLACED** by DZQ in 5.3-5.4 |
| Lossy entropy coding | **REPLACED** by RLE + Golomb-Rice in 5.5 |
| Decoder | **Host only**; floating point is fine. A measurement tool; not implemented on the FPGA |
| Decoding independence (Section 5, Decoding independence) | **[FIXED]** |

Produce an explicit **old -> new replacement map** in your plan: which existing modules, ports, registers and BRAM regions are removed, kept or newly added, and where the new blocks plug into the existing pass-2 dataflow. Reuse existing interfaces and naming from the prior system context rather than inventing new ones.

---

## 3. Input data and preprocessing invariants [FIXED]

Each MIT-BIH record is processed offline on a host:
- Single channel, **lead MLII**.
- Zero-phase 4th-order Butterworth bandpass, **0.5-40 Hz**.
- Quantized to **int16 at 200 ADU/mV**.
- Fs = 360 Hz.
- The core takes no live analog input, and BRAM cannot hold a half-hour record, so the **first 5 minutes (108,000 samples)** of each excerpt are loaded into BRAM (bitstream coefficient initialization or JTAG binary load).
- PL runs two sequential passes: **(1) detection, (2) compression + encoding + packing.**

Consequences to respect:
- Reuse the **existing segmentation and minimum-length rules** of the old HWT path wherever possible, and state in your plan where the new path differs.
- **Padding is not reused.** The new transform needs no padding (5.0 Step 2). If the existing packet format forces alignment padding, that padding carries no signal data, the decoder must recover the true segment length N, and PRD is computed on the true N samples only.
- **Follow what the existing evaluation does for strict PRD** and state it in the plan.

---

## 4. Paper facts to carry over (with confidence)

1. **Wavelet:** `bior4.4`, five-level DWT. 
2. **Levels:**  L is chosen from the sampling frequency. It yields **L = 5 for Fs = 360 Hz** (consistent with Table I). L can be calculated as floor(log2(Fs) - 2.96)
3. **Table I (record 117) subbands and energy contribution (ECE):**

| Band | Freq range (Hz) | ECE (%) |
|---|---|---|
| cD1 | (90, 180] | 0.0034 |
| cD2 | (45, 90] | 0.0101 |
| cD3 | (22.5, 45] | 0.2601 |
| cD4 | (11.25, 22.5] | 1.0742 |
| cD5 | (5.625, 11.25] | 0.8668 |
| cA5 | [0, 5.625] | 97.7854 |

4. **cD1 is discarded entirely;** cD2-cD5 and cA5 go through dead-zone quantization.
5. **Dead-zone quantizer:**
   - |c| < T maps to 0; larger values use a uniform step Delta.
   - **Delta = a * T**, with a in [1.20, 1.80]. The paper uses **a = 1.40**.
   - The exact decision intervals and reconstruction levels are given in Section 5.4.
6.  **T is global** (one value for the whole dataset), not per record. The paper's per-record header stores Delta, but we do not.
7. The paper notes the reconstruction is smoother than the input (the quantizer removes high-frequency content). With our 40 Hz low-pass already applied, expect cD1/cD2 to carry almost no energy.
---

## 5. Algorithm specification

### Role of the Python model [FIXED]
- The Python model is a **bit-accurate, integer-only model of the hardware encoder**: the golden model. It is the specification the FPGA must reproduce bit for bit. There is no separate floating-point model.
- Every PRD and CR number in this project comes from it. It therefore includes the hardware word lengths, rounding, saturation, representable values of T, Delta and 1/Delta, and buffer and segment-length limits.
- **Word lengths and constant formats are chosen up front from the resource budget** (the existing documentation has it; state the budget and the widths you assume in your plan). Narrowing them later needs my approval.
- The host decoder is only a measurement tool: it reads the coded lossy payload the model emits. It may use floating point, but its inverse transform must use the same lifting constants as the encoder.

### Order of work [FIXED]
0. **Measure only:** build the Python reference model, extract the lossy segments from pass 1, produce the segment-length histogram (5.0 Step 1).
1. **Select the global T and a FIRST (5.3).** Use one fixed reference configuration: the integer golden model at the planned hardware word lengths (T, Delta and 1/Delta take only representable fixed-point values), **boundary method (b)** (real-neighbor extension with the decoder-side solve, 5.0 Step 4) at a provisional margin M (a multiple of 2^L_max, chosen from the histogram and stated in the plan), `L_max = 5` with the default `f(N)` of 5.0 Step 3, the 5.4 quantizer, and the 5.5 coder with per-segment state reset using **provisional simple fixed Rice defaults** (stated in the plan). The PRD of every sweep point is measured through the (b) decoder solve. Freeze T and a after this step.
1b. **Retrain the trained Rice defaults at the frozen T and a** (training records only, 5.5), and re-report the CR at the frozen T with the retrained defaults. Entropy coding is lossless, so the quantized values and the PRD do not change; only CR does. The PRD-limited T from step 1 therefore stays valid. Do not re-open T here; step 3 measures any effect.
2. **Then, at the frozen T and a,** compare: boundary method (a) vs (b) (T was frozen under (b)), margin M, fixed vs adaptive depth, entropy-coder variants (including the cA_L level-only variant, 5.5 item 6), and the 5/3 ablation (which gets its own T, see 5.1a).
3. **Sensitivity check:** after the decisions in step 2, re-run the T sweep once with the chosen configuration and report how much PRD/CR would change. Do not change T without asking me.

### Decoding independence [FIXED]
Goal: no lossy segment may require the whole record, or any distant part of it, to be received before it can be decompressed. The decoding unit is **one lossy segment** (check how segments map to packets in the existing packet format and state the mapping in your plan).
1. **Payload level:** a lossy segment's payload is entropy-decoded using only its own bits. No coder state (Rice parameters, running means, run state) is carried from any other segment or packet. Every segment starts from fixed, trained default states.
2. **Reconstruction level:**
   - With boundary method (a) a segment is reconstructed from its own payload alone.
   - With boundary method (b) a segment may use **only its own payload plus the two lossless regions immediately adjacent to it** (the one before and the one after), and from each of those at most M samples, taken from the side nearest the segment. It must not use any other lossless region, any lossy region, or anything further away.
   - If an adjacent lossless region has fewer than M samples, mirror within the samples that region has; never continue past it into the next region. At the start or end of the record, where there is no neighbor, mirror as in (a).
   - Decoding a segment therefore needs a look-ahead of one region (the following lossless region). This is accepted.
3. **Long segments never relax this rule.** If a very long lossy segment is split into chunks (5.0 Step 2a), each chunk is its own decoding unit; a chunk may never use samples of another lossy chunk.
4. **[VERIFY from the existing system documentation, report in your plan]:** that each lossless region can be decoded on its own, i.e. the lossless path carries no predictor or entropy state from earlier regions. If it does, tell me before proceeding, because "adjacent lossless region" would then pull in earlier regions. Do not change the lossless path (Section 2); ask me.

### 5.0 Segment handling (adaptive system) [DECIDE, with defaults]

**Step 1: measure before designing.** From the pass-1 output on every evaluation record, produce a histogram of lossy-segment lengths (min, median, max, and the fraction below 32 / 64 / 128 / 256 or higher powers of 2 samples) and the fraction of all samples that go to the lossy path. Design decisions below depend on this histogram.

**Step 2: handle arbitrary lengths.**
- Lifting with whole-sample symmetric extension works for **any length N >= 2**; the approximation and detail bands get ceil(N/2) and floor(N/2) samples (or the reverse, depending on the phase convention; fix one and document it). No padding of any kind is needed for the transform (not to a multiple of 32, not to a multiple of 2^L), in either boundary method. In method (b) the dyadic grid alignment comes from the margin M (5.0 Step 4), not from padding the segment.
- Segments shorter than a minimum length `N_min` **[DECIDE]**: fall back to fewer levels, to the lossless path, or to a simple quantized-difference coder. Choose from measurements, and check what the old HWT path did.

**Step 2a: maximum segment length `N_max` [DECIDE, with a default policy].**
- Choose N_max from the Step 1 histogram, the working-buffer size, the width of the segment-length field in the existing packet format (it may already impose a limit; check), and the host solve cost in method (b) (A_u is N x N). If the old path had a maximum segment length, state it.
- A segment with N <= N_max is one decoding unit with both neighbors as in Step 4.
- **The neighbor rule is kept, not relaxed.** A long segment on its own does not break it: it still has exactly two adjacent lossless regions. Only splitting creates a problem, because an inner chunk would have a lossy chunk as its neighbor, and Decoding independence forbids using another lossy chunk's samples (the decoder only has approximations of them).
- A segment with N > N_max is split into chunks of length between N_min and N_max (near-equal sizes, no tiny remainder). Each chunk has its own payload. The extension type is chosen **per edge**: the outer-left edge of the first chunk and the outer-right edge of the last chunk use method (b) with the real neighbor; every inner joint uses symmetric mirror extension (method (a)) on both sides of the joint. Support per-edge selection in the encoder, the decoder solve and the A_u cache key.
- Mirroring at an inner joint sits on smooth non-QRS signal rather than a QRS flank, so I expect (not verified) the artifact cost to be small. Measure it: report the fraction of samples in segments longer than N_max and the CR/PRD cost of splitting versus a larger N_max.
- If the existing packet format cannot carry several lossy payloads for one gap, **stop and ask me**.

**Step 3: adaptive decomposition depth.**
- Choose the number of levels per segment, `L_seg = min(L_max, f(N))`, for example stop when the approximation band would fall below about 16 samples. Make `f` and `L_max` tunable and sweep them. **[DECIDE]**
- If `L_seg` is variable, it must be known to the decoder: derive it deterministically from N (no side info), or signal it in a 2-3 bit field if the packet format has room.
- Compare L_max = 3, 4, 5 on real segments. For short segments I expect (not verified) that fewer levels win, because the deep bands are dominated by boundary extension.
- **Fixed vs adaptive depth is decided after testing.** The reference model must support both through one parameter set (`L_max`, `f(N)`); fixed depth is the special case where `f(N) = L_max`.
- **Name bands by level, never by list position** (`cD_j`, and the deepest approximation band `cA_L`), because the deepest band changes with `L_seg`. Wherever this brief says cA5 or cD5, read it as the deepest approximation or detail band under the chosen depth.

**Step 4: boundary handling.** Symmetric extension is continuous but flips the slope. Both ends of a segment sit on QRS flanks, so the mirrored signal creates artificial features and inflated boundary detail coefficients, which cost both CR and PRD. Evaluate **(b) first, then (a)**, on the same segments at the frozen global T and a (Order of work; T was frozen under (b), and the sensitivity check in step 3 covers the bias this gives (b)), and compare boundary-coefficient energy, lossy-only PRD and CR before choosing. Neither is the default until that comparison is done. (c) only with my approval:
- **(a) Symmetric extension (comparison baseline, evaluated second):** whole-sample symmetric extension at both segment ends (decoder applies the same extension in its inverse; no decoder change).
- **(b) Real-neighbor extension with a decoder-side solve (evaluate this first, it is the main candidate):** instead of mirroring, extend the segment with the **actual samples of the adjacent lossless (QRS) regions**, which the host decoder already has exactly.
  - *Encoder:* take the segment plus a margin of M real samples on each side (read from BRAM). **M is a global constant that is a multiple of 2^L_max** (for example 32, 64 or 128 when L_max = 5), not a per-segment value: 2^L_seg divides 2^L_max for every L_seg <= L_max, so the dyadic grid then aligns with the segment start at every depth. The paper transforms whole records and defines no margin M; M is our design parameter **[DECIDE]**. Beyond the margin, or where the neighbor region is shorter than M, extend by mirroring the margin samples. Real samples come only from the immediately adjacent lossless region on each side; never reach past it into any other region (Decoding independence). Run the lifting transform on this extended signal, and quantize/code **only the rows whose centers fall inside the segment** (see *Counts in method (b)* below; the counts must be deterministic functions of N, L_seg, M and grid phase so the decoder derives them).
  - *Decoder (host):* decode the two lossless regions adjacent to this segment (the one before and the one after, nothing else) first. Rebuild the same extended signal using the known neighbor samples, with the segment samples unknown. Solve the linear system *A_u x_u = c_q - A_k x_k* in the least-squares sense, where *c_q* are the dequantized coefficients, *A_u* is the analysis operator restricted to the **solver equations** (all rows inside the segment, including the zero-valued cD1 rows; see *Counts in method (b)*) and the unknown samples, and *x_k* are the known samples. Cache *A_u* per (N, L_seg, M, grid phase, edge types). Do **not** use margin samples that belong to a neighboring *lossy* segment (the decoder only has approximations of them, so the encoder and decoder would disagree); use mirroring beyond the lossless margin instead. Rows that were discarded (all of cD1) or quantized to zero by the dead zone enter the system as zero-valued equations (right-hand side 0); without the cD1 rows there are only `n_coded` (about N/2) equations for N unknowns. State and test this treatment and report the conditioning of the solve. Build A_u from the linear lifting operator with the same constants the encoder uses, and treat the encoder's integer rounding as noise.
 Whether real neighbors give smaller boundary coefficients than mirroring must be measured on actual ECG segments. Also check with the golden model's actual integer lifting implementation.
  - *Costs:* the encoder transforms about N+2M samples per segment instead of N (small); the decoder solves a modest linear system per segment (host only); the segment needs only its two adjacent lossless regions decoded first, not the whole record (Decoding independence). The packet format is unchanged provided the decoder can derive the per-band counts from (N, L_seg, M); no side information is added. Confirm against the existing packet fields.

  **Counts in method (b) [FIXED definitions; keep the two counts separate in code, tests and prose]**
  - `n_coded` (**coded coefficients**): the quantized values the entropy coder must represent. These are all rows of cD_2..cD_L and cA_L whose centers lie inside the segment, **including rows quantized to zero** (they are carried inside zero runs and are not extra rows). cD1 contributes none, because it is discarded and never coded. The entropy-coder subband lengths (5.5) are the per-band parts of `n_coded`.
  - `n_eq` (**solver equations**): every analysis row whose center lies inside the segment, `n_eq = n_coded + n_cD1`. The cD1 rows are never coded but enter the decoder solve as zero-valued equations. Dead-zone rows are already counted in `n_coded` and also have right-hand side 0; they are not additional rows. A_u has `n_eq` rows.
  - Never write "the number of coefficients" without saying which of the two is meant. The per-band counts `n_cD_j` and `n_cA_L` are derived by the decoder from (N, L_seg, M, phase); they are not transmitted.
  - **Inclusion rule [DEFAULT, confirm with the golden model]:** use the lifting phase of 5.1a (low band = even samples, high band = odd samples). Index the extended signal from 0 with the segment occupying positions M..M+N-1, with M a multiple of 2^L_max. `cA_L[i]` is centered at `2^L * i` and `cD_j[i]` at `2^(j-1) * (2i+1)`. A row is inside iff its center is in [M, M+N-1]. Under this rule every segment sample is the center of exactly one row, so `n_eq = N`, `n_cD1 = floor(N/2)`, `n_coded = ceil(N/2)` and A_u is square (N x N). **[VERIFY]** with the golden model's actual integer lifting. If the model gives any other count, do not adopt it silently: report which inclusion rule produced it and ask me which rule is intended. Keep the least-squares solver even when A_u is square, so other inclusion rules still work.
- **(c) Guard samples:** let the detector's QRS window extend slightly so the lossy segment starts on a flatter part. This changes pass 1's output, so **ask me before doing this**; do not modify the detection pass on your own.

**Step 5: do not concatenate non-adjacent segments** to build longer transform inputs; the joins create discontinuities.

**Step 6: where the old path's per-segment behavior matters.** State how the old 4-level HWT handled short segments, and make sure the new path is not compared unfairly 

### 5.1 Forward transform [DEFAULT]
- **bior4.4 (CDF 9/7 family)**, applied **per lossy segment** (Section 5.0), with the number of levels per 5.0 Step 3 (up to 5 as in the paper).
- Use a **lifting implementation with whole-sample symmetric boundary extension**. With boundary method (a) this yields exactly N critically sampled coefficients for a segment of N samples (any N >= 2); method (b) is defined in 5.0 Step 4 (`n_eq` solver equations and `n_coded` coded coefficients). It maps well to hardware. The boundary method actually used is decided in 5.0 Step 4.
- Note that `pywt`'s default boundary mode gives slightly more coefficients per level and different edge behavior. **Write the Python golden model as your own integer lifting code** and use `pywt.wavedec('bior4.4')` (floating point) only as a one-time check of the lifting structure and constants on interior coefficients, up to scale, shift and sign. It is not a standing reference model. Confirm the normalization (subband gain) matches the one you use for thresholds **[VERIFY]**.
- Subband sizes for a segment of length N: about N/2, N/4, ... for cD1, cD2, ... and N/2^L for cA_L (rounded per the phase convention you fix).
- **Hardware is the constraint, so decide the arithmetic:**
  - The 9/7 lifting constants are irrational. Use fixed-point constants with enough fractional bits, or shift-add approximations if multipliers are scarce.
  - Make the decomposition depth a parameter, fixed or adaptive per segment, decided after testing (Section 5.0). The old path was 4-level; the paper is 5-level on whole records. Compare 3, 4 and 5 on real segments **[DECIDE]**.
  - The Python model is bit-accurate and integer-only (see Role of the Python model).

### 5.1a Lifting implementation of bior4.4 (the 9/7 filter pair)
`bior4.4` is the CDF 9/7 pair (9-tap analysis low-pass, 7-tap synthesis). **[VERIFY]** that the lifting scheme below with the standard constants reproduces `pywt` `bior4.4` interior coefficients up to scale, shift and sign (see 5.1). The lifting constants `alpha, beta, gamma, delta, K` below are unrelated to the quantizer symbols `a`, `Delta`, `delta` (5.4) and to the Rice `k` / `K_max` (5.5); see Notation.

Forward, for a segment x[0..N-1], on even/odd samples, using whole-sample symmetric reflection of indices at the segment ends (each step reads reflected neighbors, which is equivalent to symmetrically extending the input):
```
alpha = -1.586134342059924    beta  = -0.052980118572961
gamma =  0.882911075530934    delta =  0.443506852043971
K     =  1.230174104914001

1.  y[2n+1] = x[2n+1] + alpha * ( x[2n]   + x[2n+2]   )
2.  y[2n]   = x[2n]   + beta  * ( y[2n-1] + y[2n+1]   )
3.  y[2n+1] = y[2n+1] + gamma * ( y[2n]   + y[2n+2]   )
4.  y[2n]   = y[2n]   + delta * ( y[2n-1] + y[2n+1]   )
5.  low  (approximation) = y[even] / K          high (detail) = -K * y[odd]
```
**Reflection rule (whole-sample symmetric, edge sample not repeated):** for an index i outside 0..N-1 use `i -> -i` if i < 0, and `i -> 2*(N-1) - i` if i > N-1. Each lifting step reads only +-1 neighbors, so a single reflection per read is enough. Every level needs an input band of at least 2 samples; stop descending before a band would be shorter (see the L_seg rule in 5.0 Step 3). The same rule applies to the 5/3 ablation.

Next level repeats on the low band. Inverse = the steps in reverse order with signs flipped.

**Normalization (important because T is a fixed global value):**
- The JPEG2000-style scaling in step 5 is **not** energy-preserving (coefficient energy is expected to be about half the signal energy) **[VERIFY]**.
- `pywt` bior4.4 is: low_pywt = sqrt(2) * low_above, high_pywt = high_above / sqrt(2). In that normalization coefficient energy should match signal energy (ratio about 1.00; the transform is near-orthonormal, not exactly) **[VERIFY]**.
- **Use the pywt-style (near-orthonormal) scaling** so that T and Delta are in ADU-like units and coefficient-domain error is close to signal-domain error. State the exact per-band constants you use.
- **Hardware shortcut to evaluate [DECIDE]:** steps 1-4 need 4 constant multiplications and 8 additions per pair of samples. The final per-band scale factors can be **folded into the quantizer** (scale T and 1/Delta per band instead of scaling the coefficients), saving the scaling multipliers. Check that this is exactly equivalent across levels, including the effect of the unscaled low band feeding the next level.
  - Example: instead of computing `c = s_b * y` and testing `|c| < T`, test `|y| < T / s_b` and quantize with step `Delta / s_b`. In real arithmetic this is identical, because the quantizer only sees the product. The scalings skipped on the low band at earlier levels accumulate into the constant `s_b` of every deeper band. In fixed point it is **not** bit-identical (dynamic range and rounding change), so judge it with the bit-accurate model.
- Constants: choose fixed-point widths or shift-add (CSD) forms up front to fit the resource budget (see Role of the Python model). The decoder's inverse must use the same constants. **[DECIDE]**

**Integer alternative to compare [DECIDE, ablation]:** the reversible 5/3 pair (`bior2.2`, JPEG2000 lossless filter) is multiplier-free: `y[2n+1] = x[2n+1] - floor((x[2n]+x[2n+2])/2)`, `y[2n] = x[2n] + floor((y[2n-1]+y[2n+1]+2)/4)`
- **Re-tune T (and a) separately for the 5/3 filter** with the same sweep and training records as 5.3; never reuse the 9/7 T. Document the 5/3 band gains that define its T (its normalization differs from the 9/7 one). Compare 5/3 vs 9/7 each at its own tuned T, at matched PRD and matched CR (Section 7).

### 5.2 Subband policy [DEFAULT]
- Discard cD1 (no coefficients stored or coded; the decoder sets it to zero).
- Quantize cD2-cD5 and cA5 with one global T and Delta.
- Optional ablation **[DECIDE]**: also discard cD2 given the 40 Hz low-pass. Report the effect; do not enable by default.
- Optional ablation **[DECIDE]**: DPCM on cA5 before quantization. cA5 holds ~98% of the energy and is dense, so zero-run RLE will not help it much. Report CR/PRD effect; not default. Whether cA_L should skip RLE altogether is a separate coder variant (5.5 item 6).

### 5.3 Threshold selection [FIXED: one global T, selected first]
- **T and a are selected before any other comparison in this brief** (see Order of work). T and a are frozen under boundary method (b); boundary method, depth and coder variants are compared afterwards at the frozen T and a.
- **T is global**: one fixed threshold for **all records** and all quantized subbands, not computed per record or per segment. Delta = a * T with a = 1.40 initially (paper range 1.20-1.80). Both are **constants or global configuration registers**; nothing about T or Delta is sent per record or per segment.
- Units: the host quantizes every record to int16 at the same 200 ADU/mV, so a single T expressed in ADU is meaningful across records **provided the coefficient normalization is fixed and documented** (Section 5.1a). Define T in that normalization.
- **How to pick the global T (host-side, offline):** sweep T (and a), using only values representable in the hardware's fixed-point format for T, Delta and 1/Delta, over a **training set of records**. **The sweep is driven by lossy-only PRD** (Section 7 metric (ii)), not whole-record PRD: for every (T, a) compute the lossy-regions-only strict PRD and the lossy-path CR through the reference configuration of Order of work step 1 (including the (b) decoder solve), and choose the largest T (highest CR) whose lossy-only PRD still meets the target, for example mean lossy-only PRD <= target. Whole-record PRD is reported but never used to select T. Also report the worst-record lossy-only PRD. The Rice defaults are provisional during this sweep (5.5); they change CR only, not PRD, so they do not move the PRD-limited T. A single T gives different PRD/CR on different records (amplitude, noise, arrhythmia), so report the **spread across records**, not just the mean. Then **validate on held-out records** to check that the chosen T generalizes.
- **EPE (the paper's rule) is only an initial guess**, not a requirement: it is the paper's way of bounding the energy discarded by thresholding. If you use it, pool the lossy-segment coefficients of the training records only (never QRS-region energy, which would dominate), and compare it against the direct PRD-driven sweep above. EPE ignores the quantization error from Delta and is a coefficient-domain proxy, so the direct sweep (using the reference decoder) is the more reliable criterion.

- **Hardware:** T, Delta and a fixed-point 1/Delta are compile-time constants (or one-time configuration). No on-chip sorting, histogram, energy computation or division.

### 5.4 Dead-zone quantization [FIXED: taken from the paper's Eq. 3-4]
The paper (Section II-C, Eq. 3 and 4), with delta = Delta/2 (a local half-step symbol used only in this block; it is **not** the lifting constant `delta` of 5.1a, see Notation):
```
Decision intervals I_k:   k = -1 : (-3*delta, -T]
                          k =  0 : (-T, T)
                          k = +1 : [T, 3*delta)
                          else   : [(2k-1)*delta, (2k+1)*delta)
Reconstruction levels O_k: 0 for k = 0;   k * Delta for k = +-1, +-2, ...
```
Equivalent implementation (valid whenever T >= Delta/2, which holds for a <= 2):
```
|c| <  T :   q = 0
|c| >= T :   q = sign(c) * max(1, floor(|c|/Delta + 0.5))      # uniform rounding outside the dead zone
host dequantization:   c_hat = q * Delta
```
- The zero bin is the wider dead zone (-T, T), and the first nonzero bin is [T, 1.5*Delta), reconstructed at Delta.
- Hardware needs one compare against T, one multiply by 1/Delta, one add of 0.5 and a floor. No division.
- Optional decoder-only ablation, off by default: shrink reconstruction toward zero, c_hat = sign(q)*(|q| - theta)*Delta with theta in [0, 0.2]. The paper uses theta = 0.

### 5.5 Entropy coding [FIXED structure, DECIDE details]
Applied **independently per subband** within each segment (runs never cross a subband or segment boundary). Coding order within a segment: coarsest approximation band first, then details from coarse to fine.

**Short segments make warm-up costly:** with only a handful of coefficients in the deepest bands, a Rice parameter that restarts per segment wastes bits. Because of the Decoding independence requirement, **do not carry Rice state between segments.** Reset the adaptive state at the start of every segment to a **trained default per band level** (a constant table trained offline on the training records; it may depend on band level and on L_seg). Compare trained defaults against simple fixed defaults and against forward-selected k (item 5) **[DECIDE]**. The table depends on T and a, so: during the T sweep use provisional simple fixed defaults (state them in the plan), then retrain the table at the frozen T and a (Order of work step 1b), on training records only. Entropy coding is lossless, so this affects CR only, never PRD. The decoder mirrors the same reset.

1. **Zero-run RLE:** convert each subband's quantized sequence into tokens `(zero_run, level)` where `level != 0`. Subband lengths are known to the decoder, so a trailing run of zeros needs no end-of-block marker if you define the stream unambiguously. Specify exactly how trailing zeros and an all-zero subband are represented.
2. **Zigzag mapping:** `z = (v << 1) ^ (v >> 15)` (0 -> 0, -1 -> 1, +1 -> 2, ...). Since a token's level is never 0, you may code `z - 1` to save one codeword value **[DECIDE]**.
   - Note the cost of the sign is 1 bit per nonzero
3. **Golomb-Rice coding** (`m = 2^k`): unary quotient `m >> k`, then `k` LSBs. Two parameters per subband: `k_run` for zero-run lengths and `k_lvl` for the zigzagged levels.
4. **Outliers [FIXED requirement]:** use a **length-limited Rice code** with an escape (e.g., after Q_ESC ones in the unary part, emit the value raw in B bits), so a rare huge run or level cannot create an unbounded code. Choose Q_ESC and B and justify them.
5. **How k is chosen [DECIDE], compare in Python and recommend:**
   - **Backward-adaptive** per subband (running mean, JPEG-LS style): single pass, no side information, decoder mirrors the update. My recommendation for streaming hardware.
   - **Forward-selected** per subband: the encoder tries k = 0..K_max (cost is a sum of bit lengths) and stores k in side info. Costs an extra statistics pass and memory.
   - Report CR for both. If forward-selected wins by a meaningful margin, state what extra BRAM/passes it needs.
6. **cA_L coding variant [DECIDE, compare at the frozen T and a]:** cA_L is dense (about 98% of the energy, few zeros after quantization), so `(zero_run, level)` tokens may spend a run codeword on almost every value. Evaluate:
   - (i) the standard RLE path for cA_L (baseline);
   - (ii) **level-only**: no run tokens; every cA_L value is coded directly as a zigzagged level with Rice parameter `k_lvl` (zero is a legal value here, so the `z - 1` saving of item 2 does not apply on this path; no `k_run` is needed);
   - (iii) level-only combined with the DPCM ablation of 5.2.
   Keep the detail bands on RLE. The choice is fixed for all segments (no per-segment flag, so no side information); a per-segment flag would need a side-info bit, so ask me first (5.7). Report CR for each variant and the hardware logic each adds or removes.

### 5.6 Reconstruction (host only)
Parse the payload -> Rice decode -> undo zigzag -> expand zero runs -> dequantize (5.4) -> zero cD1 -> inverse lifting transform (floating point is fine, but with the same lifting constants as the encoder) -> reconstructed signal. This decoder only measures what the encoder produced; it is not part of the hardware. Provide a bit-exact check that decode(encode(q)) == q for every record. If boundary method (b) is chosen, replace the inverse lifting of each lossy segment by the least-squares solve in 5.0 Step 4(b), run once the two adjacent lossless regions are decoded.

### 5.7 Side information and packet format [FIXED format, DECIDE placement]
- Side info the decoder needs: **nothing per record for T and Delta** (they are global constants known to both sides; store them in a global config/version field only if the format already has one); escape parameters if not fixed; **per segment** the length (probably already in the packet) and L_seg if not derivable from the length; per-subband Rice parameters only if forward-selected (these are expensive per short segment, which favors backward-adaptive k). Enumerate it with a bit budget.
- Fit it into the **existing header/packet fields**. If something does not fit, **stop and ask me** instead of changing the format.
- The paper's header (16-bit Delta + a Huffman dictionary) is **not** needed in our design.
- Nothing in a segment's payload or side info may refer to another segment's or region's data (Decoding independence).

---

## 6. Hardware considerations
- The hardware constraints and the resources already used by the rest of the pipeline are in your previous context. **Give a resource estimate (BRAM, DSP, LUT/FF, latency/cycles) for the new blocks**, and compare it with what the old HWT + coder used and with what remains. If something exceeds the remaining budget, propose cheaper alternatives (fewer levels, shift-add constants, shorter filters).
- The forward transform and the quantizer should be streamable or in place, because BRAM is already largely used by the 108,000-sample input. State where the coefficients live between the transform and the coder, and the peak memory.
- Decide which operations belong in pass 2 and confirm pass 1 (detection) is unaffected.
- Every hardware block must match the Python golden model bit for bit at its stage outputs; the model produces the test vectors.

---

## 7. Metrics and evaluation protocol
- Report **two sets of every distortion metric**: (i) **whole-record** (lossless QRS regions contribute zero error but their energy sits in the denominator, which lowers PRD) and (ii) **lossy-regions-only** (stricter, and the fairer measure of the lossy coder). Also report **CR of the lossy path alone** (bits in vs bits out for lossy segments) 
- Metrics: **CR, strict PRD, SNR, QS = CR/PRD, QSN = CR/PRDN**, plus RMSE in ADU. Use the paper's definitions,  Use the **same CR definition (raw bits in the numerator) as the existing system** 
.
- Records: use the **same record set as the existing evaluation** 
- Compare **old HWT path vs new path**:
  1. **Matched PRD:** CR of each at the same PRD (per record and average).
  2. **Matched CR:** PRD of each at the same CR.
  3. Full CR-PRD sweep curves (global-T and a sweeps) per record and the average.
- Show where the PRD improvement comes from by ablating: wavelet only (new transform + old quantizer/coder), then the new quantizer, then the new coder.
- Report the effect of the optional reconstruction shrinkage `theta`.
- Report the CR cost of resetting the Rice state per segment (against a persistent-state run, for information only).
- Report any records that behave badly (arrhythmias, noisy records) rather than hiding them behind the average.

---

## 8. Open decisions you must resolve or ask about (recommended defaults in brackets)

1. Number of levels, 4 or 5, and fixed vs adaptive per segment? [decided after testing; 5 per paper, parameterized; compare]
2. Segment-based transform: minimum segment length, fallback for short segments, adaptive L_seg rule, boundary method? [measure segment lengths first; boundary method decided by comparing (b) then (a); fallback decided from data]
3. Forward-selected vs backward-adaptive Rice k? [backward-adaptive, compare]
4. Whether T/EPE includes cD1 and whether cA5 gets DPCM? [cD1 excluded, no DPCM by default]
5. Where the new side info fits in the existing packet header.
6. Fixed-point word lengths and formats for transform constants, intermediates and 1/Delta (set up front from the resource budget).
7. Rice-state initialization at the start of each segment: trained default per band level vs simple fixed default? [trained default; no state is carried across segments, see Decoding independence]. (T is global, decided.)
8. Whether pass 1 may be given a guard-sample parameter (requires my approval; Section 5.0 Step 4c).
9. Boundary method: symmetric (a) vs real-neighbor extension with decoder solve (b). Compare on real segments. Margin M, and the mirror fallback beyond the lossless margin.
10. Global T selection (done first, see Order of work): dataset-level PRD-driven sweep vs EPE. [lossy-only-PRD-driven sweep; EPE as initial guess]; training/validation split.
11. Maximum segment length `N_max` and the split policy for longer segments (5.0 Step 2a).

If a decision needs information that is not in this brief or your prior context, ask me; do not guess.

---

## 9. Verification and reporting
1. A testbench that matches the Python golden model's test vectors exactly.
2. **Integration report:** resource use, throughput, end-to-end CR/PRD on all records compared with the old path.

## 10. Acceptance criteria
- Round trip is bit-exact for the entropy coder (decode(encode(q)) == q) on every record.
- Independence tests: every lossy segment's payload decodes using only its own bits (test: decode segments one at a time, in random order). If (b) is chosen, every lossy segment reconstructs identically when given only its payload and its two adjacent lossless regions, compared with a full-record decode.
- Hardware output matches the Python golden model bit for bit, at every stage output and in the final packets.
- On-board check: capture the real FPGA packets, decode them with the Python decoder, and confirm the decoded PRD equals the golden model's.
- At the target operating point (**whole-record** PRD): PRD <= 2 and CR >= 5, or a clear written tradeoff curve showing why not.
- Lossless path, detection pass and packet format are verified unchanged by regression tests.
- Resource usage fits the remaining budget , or the overrun is documented with alternatives.

## 11. Things not to assume
- If a detail is missing or unreadable, say so and ask.
- Do not change the packet format, the detection pass or the lossless path without asking.
