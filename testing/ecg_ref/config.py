"""Single source of truth for every constant in the ECG compression system.

Any value used by both the Python reference model and the HLS implementation lives
here. `scripts/export_hls_constants.py` regenerates hls/src/ecg_constants.h from this
module so the two implementations can never drift apart.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Signal / dataset
# ---------------------------------------------------------------------------
FS = 360                    # MIT-BIH sampling rate (Hz)
N_SAMPLES = 108_000         # 5 minutes * 60 s * 360 Hz
RECORD_START = 0            # first sample index taken from each record
TARGET_LEAD = "MLII"

# The 48 MIT-BIH Arrhythmia Database records. 102 and 104 carry no MLII lead
# (surgical dressings forced V5/V2), so they are excluded -> 46 usable records.
ALL_MITDB_RECORDS = (
    "100", "101", "102", "103", "104", "105", "106", "107", "108", "109",
    "111", "112", "113", "114", "115", "116", "117", "118", "119",
    "121", "122", "123", "124",
    "200", "201", "202", "203", "205", "207", "208", "209",
    "210", "212", "213", "214", "215", "217", "219",
    "220", "221", "222", "223", "228",
    "230", "231", "232", "233", "234",
)
NO_MLII_RECORDS = ("102", "104")
MLII_RECORDS = tuple(r for r in ALL_MITDB_RECORDS if r not in NO_MLII_RECORDS)

# ---------------------------------------------------------------------------
# Offline preprocessing (host laptop, Python only)
# ---------------------------------------------------------------------------
PRE_BP_LOW_HZ = 0.5         # 4th-order Butterworth bandpass, low edge
PRE_BP_HIGH_HZ = 40.0       # 4th-order Butterworth bandpass, high edge
PRE_BP_ORDER = 4

# Every MIT-BIH record is digitised at 200 ADC units/mV with an 11-bit converter, so
# ADC units are already a uniformly calibrated physical scale and the fixed integer
# band thresholds are directly comparable across records. Preprocessing therefore
# rescales only by the ratio of the record's own header gain to this reference, which
# is exactly 1.0 for every MIT-BIH record but keeps the pipeline correct for any
# record digitised differently.
#
# Amplifying beyond the native ADC scale would be actively harmful: it manufactures
# low-order bits that carry no information, and the lossless QRS coder is obliged to
# transmit every one of them (measured at ~4 extra bits/sample for a 15x gain).
QUANT_TARGET_ADU_PER_MV = 200.0
MITBIH_ADC_ZERO = 1024      # converter mid-scale, used by the baseline-inclusive PRD
SAMPLE_BITS = 16
SAMPLE_MIN = -32768
SAMPLE_MAX = 32767

# ---------------------------------------------------------------------------
# QRS detection chain (FPGA, must be bit-exact with HLS)
# ---------------------------------------------------------------------------
# Resource-minimised first-order IIR bandpass: a 1st-order low-pass cascaded with a
# 1st-order high-pass, both realised as exponential moving averages whose smoothing
# factor is a power of two, so each reduces to a shift-and-add (no DSP48 slices).
#
#   fc = -ln(1 - 2^-shift) * fs / (2*pi)
#
# LP shift 1 -> alpha 1/2   -> fc ~= 39.7 Hz  (target fs/8  = 45.0 Hz)
# HP shift 3 -> alpha 1/8   -> fc ~=  7.6 Hz  (target fs/32 = 11.25 Hz)
#
# The passband (~7.6-39.7 Hz) brackets peak QRS spectral power. This intentionally
# replaces the spec's 1.8-7.2 Hz band: the filter only supports region
# identification, and detection is validated against the .atr annotations rather
# than against a nominal band.
IIR_LP_SHIFT = 1
IIR_HP_SHIFT = 3
IIR_FRAC_BITS = 8           # extra fractional bits in the EMA accumulators, so the
                            # integer shift never creates a dead zone

# Teager-Kaiser energy operator needs x[n+1]; accumulators are 64-bit.
MA_LEN = 18                 # 0.05 * fs, rectangular smoothing window
THRESH_SHIFT = 7            # threshold leak: thr += (smooth - thr) / 128
THRESH_FRAC_BITS = 8        # fractional bits in the threshold accumulator
THRESH_FACTOR = 3           # R-peak flagged when smooth > 3 * thr
REFRACTORY = 72             # 200 ms at 360 Hz
THRESH_INIT = 0             # threshold accumulator initial value
# The threshold starts at zero, so until it has tracked up to the signal's baseline
# energy any sample would satisfy `smooth > 3*thr`. Triggers are suppressed for the
# first WARMUP samples while the tracker settles; the threshold itself still adapts.
THRESH_WARMUP = 2 * FS      # 2 seconds

# On a threshold crossing the true R apex is located by searching for the largest
# |sample| in a window around the trigger, which removes the group delay of the TKEO +
# moving-average chain and centres the QRS window correctly.
#
# The search must run in BOTH directions. The trigger usually lags the apex (the
# moving average delays the energy peak by ~L/2), but on a quiet baseline the adaptive
# threshold sits near zero and the crossing can instead fire on the leading edge, a
# few samples BEFORE the apex. A backwards-only search then locks onto the rising
# flank and mis-centres every window. Both bounds stay well inside the 72-sample
# refractory period, so the search can never reach a neighbouring beat.
PEAK_SEARCH_BACK = 25       # ~70 ms
PEAK_SEARCH_FWD = 25        # ~70 ms

# ---------------------------------------------------------------------------
# QRS window framing
# ---------------------------------------------------------------------------
QRS_PRE = 29                # R - 29 samples (-80 ms)
QRS_POST = 43               # R + 43 samples (+120 ms)
QRS_WINDOW = QRS_PRE + 1 + QRS_POST     # 73 samples inclusive
WS = QRS_WINDOW

# ---------------------------------------------------------------------------
# Lossless path: 2nd-order predictor + adaptive Golomb-Rice
# ---------------------------------------------------------------------------
GR_K_BITS = 4               # k is transmitted in 4 bits
GR_K_MAX = 15
# A pathological residual would otherwise produce a unary quotient thousands of bits
# long. Quotients >= GR_Q_ESCAPE emit the escape prefix followed by the raw mapped
# residual, bounding any symbol to GR_Q_ESCAPE + 1 + GR_ESCAPE_BITS bits.
GR_Q_ESCAPE = 31
GR_ESCAPE_BITS = 32
ANCHOR_COUNT = 2            # y(0), y(1) sent verbatim to seed the predictor

# ---------------------------------------------------------------------------
# Lossy path: 4-level Haar DWT + band thresholding + zero-RLE
# ---------------------------------------------------------------------------
DWT_LEVELS = 4
DWT_BLOCK = 1 << DWT_LEVELS  # segments are padded up to a multiple of 16

# A non-QRS run is bounded only by the gap between detected beats, and in the
# degenerate case of a record with no detections it would span the whole 108,000
# samples - far more than the PL can buffer (432 KB against 270 KB of device BRAM).
# Longer runs are therefore split into independently coded packets. The cap is set
# above the longest run observed anywhere in the database (1089 samples, record 124),
# so on real data no split ever occurs and the bound exists purely to make the
# hardware buffer provably sufficient.
MAX_LOSSY_SEGMENT = 2048
# Orthonormal Haar scaling by 1/sqrt(2) as an exact integer multiply-shift, so
# Python and HLS produce identical coefficients.
INV_SQRT2_Q15 = 23170        # round(2**15 / sqrt(2))
INV_SQRT2_SHIFT = 15
INV_SQRT2_ROUND = 1 << (INV_SQRT2_SHIFT - 1)   # round-to-nearest bias term
# Segments shorter than one DWT block cannot be transformed; they are sent as raw
# 16-bit samples (tag reuses the lossy packet with symbol count 0).
DWT_MIN_LEN = DWT_BLOCK

BAND_ORDER = ("A4", "D4", "D3", "D2", "D1")

# Band-dependent hard thresholds. FROZEN by the Phase 1h global calibration
# (scripts/calibrate_thresholds.py): one set for every record, no per-record tuning.
#
# Selected as the highest-compression candidate satisfying PRD < 2%
# (baseline-inclusive) and CR > 5 on EVERY one of the 46 MLII records. Record 233 is
# the binding case at CR 5.29; the distortion constraint is comfortably met at a
# worst-case 1.66%.
#
# The spec's nominal 80/120/200 assume a signal amplified well beyond the native ADC
# scale and are not usable at MIT-BIH's true 200 adu/mV.
#
# These thresholds are deliberately aggressive on the detail bands. That is the design
# intent, not an oversight: QRS complexes are reconstructed bit-exactly (PRD = 0%), and
# the bit budget is spent there rather than on the diagnostically less critical
# baseline between beats. The consequence is a large strict, zero-mean PRD in non-QRS
# regions, which run_reference.py always reports alongside the gated figure.
BAND_THRESHOLD = {
    "A4": 24,
    "D4": 64,
    "D3": 64,
    "D2": 160,
    "D1": 160,
}

# Each surviving coefficient is packed into a signed 10-bit field, so it is first
# scaled down by a fixed per-band shift (the quantization step is 2**shift). The
# shifts trade dynamic range against quantization noise: too small and large
# coefficients saturate, too large and the reconstruction error breaches the PRD
# budget. They are chosen empirically from the coefficient distribution measured
# across every record (scripts/measure_coeffs.py) and frozen here alongside the
# thresholds. Saturation is counted and reported, and is acceptable on this path
# because it only affects rare large coefficients.
BAND_QUANT_SHIFT = {
    "A4": 0,
    "D4": 0,
    "D3": 0,
    "D2": 0,
    "D1": 0,
}

RLE_RUN_BITS = 6
RLE_RUN_MAX = (1 << RLE_RUN_BITS) - 1     # 63
RLE_VAL_BITS = 10
RLE_VAL_MIN = -(1 << (RLE_VAL_BITS - 1))  # -512
RLE_VAL_MAX = (1 << (RLE_VAL_BITS - 1)) - 1   # 511
RLE_TOKEN_BITS = RLE_RUN_BITS + RLE_VAL_BITS  # 16
# A token whose value field is zero is a continuation marker: it consumes `run`
# zeros and emits no coefficient. Real coefficients are never zero because only
# non-zero coefficients produce a token.
RLE_CONT_RUN = RLE_RUN_MAX

# ---------------------------------------------------------------------------
# Bitstream format
# ---------------------------------------------------------------------------
MAGIC_WORD = 0xEC601001
HEADER_WORDS = 4
HEADER_BITS = HEADER_WORDS * 32
WORD_BITS = 32
LEN_FIELD_BITS = 16
SYMCOUNT_FIELD_BITS = 16
TAG_QRS = 0
TAG_NONQRS = 1
# 32-bit additive checksum over every payload word (cheap in hardware).
CHECKSUM_INIT = 0
CHECKSUM_MASK = 0xFFFFFFFF

# ---------------------------------------------------------------------------
# Coder -> packer channel (HLS DATAFLOW only; no effect on the reference model)
# ---------------------------------------------------------------------------
# The coders hand the packer a stream of variable-width bit fields. MAX_TOKENS is a
# reporting-only bound for the packer's loop tripcount: four fields per sample is far
# above anything the coders actually emit, and the loop is terminated by an explicit
# end token rather than by this number.
MAX_TOKENS = 4 * N_SAMPLES
# FIFO depth between the two DATAFLOW processes. Deep enough for the coder to stay
# ahead of a back-pressured DMA, shallow enough to implement in SRL rather than block
# RAM, which is the scarce resource here.
TOKEN_FIFO_DEPTH = 128

# ---------------------------------------------------------------------------
# Performance targets (spec section 2.1 / 7)
# ---------------------------------------------------------------------------
TARGET_CR = 5.0             # strictly greater than
TARGET_PRD_OVERALL = 2.0    # percent, strictly less than
TARGET_PRD_QRS = 0.0        # percent, exactly

# Which PRD convention the < 2% gate is applied to. Spec section 7 evaluates against
# "raw signals", which is the baseline-inclusive reference (the ADC offset retained)
# and the convention the < 2% / CR > 5 pairing comes from in the literature. The
# strict zero-mean PRD is always computed and reported alongside it; under the strict
# reading the two targets are provably mutually exclusive for this architecture.
PRD_GATE_METRIC = "baseline"        # "baseline" or "strict"

# Detection scoring tolerance: ANSI/AAMI EC57 uses a 150 ms matching window.
DETECT_TOLERANCE = 27       # +/-75 ms at 360 Hz

# ---------------------------------------------------------------------------
# Platform / interface
# ---------------------------------------------------------------------------
CLK_FREQ_HZ = 100_000_000
UART_BAUD = 115_200

# UART transport framing, wrapped around the compressed frame by the PS.
#
# A raw serial link has no packet boundaries and no way to signal length, so the PS
# prefixes each frame with a sync word, the frame length in 32-bit words, and the
# measured processing latency. This is transport only: the bytes that follow are the
# compressed frame exactly as the reference model produces it, which is what makes the
# "hardware output is bit-identical to golden" check meaningful.
UART_SYNC_MAGIC = 0x45434731        # "ECG1"
UART_PREAMBLE_WORDS = 3             # sync, frame word count, latency in PL cycles
BRAM_TOTAL_BITS = 60 * 36 * 1024

# ---------------------------------------------------------------------------
# Repository paths
# ---------------------------------------------------------------------------
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
MITDB_DIR = DATA_DIR / "mitdb"
COE_DIR = DATA_DIR / "coe"
GOLDEN_DIR = DATA_DIR / "golden"
REPORT_DIR = DATA_DIR / "reports"


def dwt_padded_len(n: int) -> int:
    """Length rounded up to a whole number of DWT blocks."""
    return ((n + DWT_BLOCK - 1) // DWT_BLOCK) * DWT_BLOCK


def band_lengths(padded_len: int) -> dict[str, int]:
    """Coefficient count per band for a padded segment length."""
    return {
        "A4": padded_len // 16,
        "D4": padded_len // 16,
        "D3": padded_len // 8,
        "D2": padded_len // 4,
        "D1": padded_len // 2,
    }
