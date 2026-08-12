#ifndef ECG_LOSSY_H
#define ECG_LOSSY_H

#include "bitpack.h"
#include "ecg_types.h"

// Length rounded up to a whole 4-level DWT block (multiple of 16).
static inline int ecg_dwt_padded_len(int n) {
#pragma HLS INLINE
    return ((n + ECG_DWT_BLOCK - 1) / ECG_DWT_BLOCK) * ECG_DWT_BLOCK;
}

// One level of the reversible integer Haar (lifting), in place over buf[0..len-1].
// Output layout is approximation in the low half, detail in the high half.
void ecg_haar_level(ecg_coeff_t* buf, int len, ecg_coeff_t* scratch);

// 4-level decomposition in place. The resulting layout is exactly the transmission
// order A4 | D4 | D3 | D2 | D1, so no reshuffle is needed before run-length coding.
void ecg_haar_forward(ecg_coeff_t* buf, int padded_len, ecg_coeff_t* scratch);

// Hard-threshold, scale and saturate one band region into the signed 10-bit field.
// Returns the number of coefficients that saturated.
int ecg_quantize_region(ecg_coeff_t* buf, int offset, int count,
                        int threshold, int shift);

// Threshold, quantize and run-length code one non-QRS segment, emitting the packet
// through `out` (token stream or inline packer). `buf` and `scratch` must each hold
// ECG_MAX_LOSSY_SEGMENT coefficients. Returns the number of emitted RLE tokens.
int ecg_encode_nonqrs(const ecg_bram_word_t* samples, ecg_index_t start, int length,
                      ecg_coeff_t* buf, ecg_coeff_t* scratch, ecg_coder_out& out,
                      int* saturated);

// Module-level synthesis top: token-stream sink (used by module_lossy.tcl).
int ecg_encode_nonqrs_top(const ecg_bram_word_t* samples, ecg_index_t start, int length,
                          ecg_coeff_t* buf, ecg_coeff_t* scratch,
                          hls::stream<ecg_token>& tokens, int* saturated);

#endif  // ECG_LOSSY_H
