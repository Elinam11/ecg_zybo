// Shared fixed-width types and integer helpers for the ECG compression core.
//
// The data path is expressed entirely in explicitly sized integers with explicit
// shifts, which is what makes the HLS core bit-exact against the Python reference
// model. Two build flavours are supported from one source:
//
//   * Vitis HLS synthesis / csim  (ap_int<> available, hls::stream<> available)
//   * native g++ C-simulation     (ECG_NATIVE defined; see hls/shim)
//
// Native builds exist so every module testbench can be run and diffed against the
// golden vectors on a machine without the Xilinx toolchain. The arithmetic is
// identical because it only ever uses int16_t/int32_t/int64_t and shifts.
#ifndef ECG_TYPES_H
#define ECG_TYPES_H

#include <stdint.h>

#include "ecg_constants.h"

#ifdef ECG_NATIVE
#include "hls_stream_shim.h"
#else
#include "hls_stream.h"
#include "ap_int.h"
#include "ap_axi_sdata.h"
#endif

// ---------------------------------------------------------------------------
// Data path widths
// ---------------------------------------------------------------------------
typedef int16_t ecg_sample_t;    // preprocessed ECG sample (logical sample width)
typedef uint32_t ecg_bram_word_t; // physical BRAM word: two packed int16 samples
typedef int32_t ecg_filt_t;      // IIR accumulators and bandpass output
typedef int64_t ecg_energy_t;    // TKEO / moving-average sums (squares of samples)
typedef int32_t ecg_coeff_t;     // Haar coefficients and prediction residuals
typedef uint32_t ecg_word_t;     // AXI4-Stream payload word
typedef int32_t ecg_index_t;     // sample indices

// True dual-port blk_mem_gen in Vivado 2024.2 rejects asymmetric 16-bit port B when
// port A is 32-bit. Both ports are therefore 32-bit; samples are packed little-endian
// half-words (sample 2i in [15:0], sample 2i+1 in [31:16]) to match gen_coe.py.
#define ECG_N_BRAM_WORDS (ECG_N_SAMPLES / 2)

static inline ecg_sample_t ecg_bram_read(const ecg_bram_word_t* bram, ecg_index_t i) {
#pragma HLS INLINE
    const uint32_t word = bram[i >> 1];
    const uint16_t half = (uint16_t)((i & 1) ? (word >> 16) : (word & 0xFFFFu));
    return (ecg_sample_t)half;
}

// A 32-bit output word plus its end-of-frame marker.
//
// For synthesis this must be a real AXI4-Stream type: a plain struct on an axis
// interface is packed as payload and would never produce a TLAST signal. ap_axiu also
// carries TKEEP/TSTRB, which AXI DMA honours - leaving them zero makes the DMA treat
// every byte as a null byte and silently drop the transfer.
#ifdef ECG_NATIVE
struct ecg_stream_word {
    ecg_word_t data;
    bool last;
};
#else
typedef ap_axiu<32, 0, 0, 0> ecg_stream_word;
#endif

static inline void ecg_stream_set(ecg_stream_word& word, ecg_word_t data, bool last) {
#pragma HLS INLINE
    word.data = data;
    word.last = last;
#ifndef ECG_NATIVE
    word.keep = -1;      // all four bytes valid
    word.strb = -1;
#endif
}

// Status returned to the PS over AXI4-Lite once a run completes.
struct ecg_status {
    uint32_t word_count;    // payload words streamed (header excluded)
    uint32_t qrs_count;     // QRS windows encoded
    uint32_t checksum;      // 32-bit additive checksum over the payload words
    uint32_t peak_count;    // R peaks detected
    uint32_t saturated;     // coefficients clipped by the 10-bit field
};

// ---------------------------------------------------------------------------
// Coder -> packer channel
// ---------------------------------------------------------------------------
//
// The two coders describe what they want written as a sequence of variable-width bit
// fields. Module tests still use a token stream into the packer; ecg_top packs those
// fields inline so no deep token FIFO is required on the XC7Z010.
enum ecg_token_op {
    ECG_OP_BITS  = 0,   // append the low `nbits` of `value`
    ECG_OP_ALIGN = 1,   // zero-pad to the next 32-bit boundary
    ECG_OP_QRS   = 2,   // report the QRS window count
    ECG_OP_SAT   = 3,   // report the saturated-coefficient count
    ECG_OP_PEAKS = 4,   // report the detected peak count
    ECG_OP_END   = 5    // no more tokens: flush and mark TLAST
};

struct ecg_token {
    uint32_t value;
    uint8_t nbits;      // 0..32, meaningful only for ECG_OP_BITS
    uint8_t op;
};

static inline void ecg_put_bits(hls::stream<ecg_token>& tokens, uint32_t value,
                                int nbits) {
#pragma HLS INLINE
    ecg_token t;
    t.value = value;
    t.nbits = (uint8_t)nbits;
    t.op = ECG_OP_BITS;
    tokens.write(t);
}

static inline void ecg_put_op(hls::stream<ecg_token>& tokens, uint8_t op,
                              uint32_t value) {
#pragma HLS INLINE
    ecg_token t;
    t.value = value;
    t.nbits = 0;
    t.op = op;
    tokens.write(t);
}

static inline void ecg_put_align(hls::stream<ecg_token>& tokens) {
#pragma HLS INLINE
    ecg_put_op(tokens, ECG_OP_ALIGN, 0);
}

// ---------------------------------------------------------------------------
// Integer helpers, mirroring python/ecg_ref/fixedpoint.py
// ---------------------------------------------------------------------------

// Arithmetic shift right == floor(value / 2^shift), including for negatives.
// C++20 guarantees this for signed types and every real compiler already did.
static inline int64_t ecg_asr(int64_t value, int shift) {
#pragma HLS INLINE
    return value >> shift;
}

// floor division for a positive denominator. Plain C++ '/' truncates toward zero,
// so negative numerators need an explicit correction to match Python's '//'.
static inline int64_t ecg_floor_div(int64_t numerator, int64_t denominator) {
#pragma HLS INLINE
    int64_t q = numerator / denominator;
    if ((numerator % denominator != 0) && ((numerator < 0) != (denominator < 0))) {
        q--;
    }
    return q;
}

// High 64 bits of an unsigned 64x64 product (no 128-bit type required).
static inline uint64_t ecg_mulhi_u64(uint64_t u, uint64_t v) {
#pragma HLS INLINE
    const uint64_t u0 = u & 0xffffffffULL;
    const uint64_t u1 = u >> 32;
    const uint64_t v0 = v & 0xffffffffULL;
    const uint64_t v1 = v >> 32;
    const uint64_t w0 = u0 * v0;
    uint64_t t = u1 * v0 + (w0 >> 32);
    const uint64_t w1 = t & 0xffffffffULL;
    const uint64_t w2 = t >> 32;
    t = u0 * v1 + w1;
    return u1 * v1 + w2 + (t >> 32);
}

// floor(n / 18), bit-exact with ecg_floor_div(n, 18).
//
// A literal `n / 18` becomes a pipelined 64-bit divider under `#pragma HLS PIPELINE
// II=1` on the detection loop and synthesises to thousands of LUTs (csynth showed
// ecg_energy_step alone at ~46% of the XC7Z010). Multiply-high by ceil(2^64/18)
// maps to ordinary multipliers / shifts instead.
static inline int64_t ecg_floor_div18(int64_t n) {
#pragma HLS INLINE
    const uint64_t M = 0x0E38E38E38E38E39ULL;  // floor(2^64/18) + 1 == ceil(2^64/18)
    if (n >= 0) {
        return (int64_t)ecg_mulhi_u64((uint64_t)n, M);
    }
    const int64_t t = (int64_t)ecg_mulhi_u64((uint64_t)(-n), M);  // trunc((-n)/18)
    int64_t q = -t;
    if ((n - q * 18) != 0) {
        q--;
    }
    return q;
}

static inline int32_t ecg_clamp(int32_t value, int32_t low, int32_t high) {
#pragma HLS INLINE
    if (value < low) return low;
    if (value > high) return high;
    return value;
}

// Map a signed residual onto the non-negative integers (spec 4.2.2).
static inline uint32_t ecg_zigzag(int32_t error) {
#pragma HLS INLINE
    return (error >= 0) ? ((uint32_t)error << 1)
                        : (uint32_t)((-(int64_t)error << 1) - 1);
}

static inline int32_t ecg_unzigzag(uint32_t mapped) {
#pragma HLS INLINE
    return (mapped & 1u) ? -(int32_t)((mapped + 1u) >> 1) : (int32_t)(mapped >> 1);
}

// floor(log2(value)) for value >= 1, else 0. A leading-zero count in hardware.
static inline int ecg_floor_log2(uint64_t value) {
#pragma HLS INLINE
    if (value == 0) return 0;
    int result = 0;
    while (value > 1) {
        value >>= 1;
        result++;
    }
    return result;
}

#endif  // ECG_TYPES_H
