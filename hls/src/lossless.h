#ifndef ECG_LOSSLESS_H
#define ECG_LOSSLESS_H

#include "bitpack.h"
#include "ecg_types.h"

// 2nd-order linear predictor: y_hat(n) = 2*y(n-1) - y(n-2).
static inline int32_t ecg_predict(int32_t prev1, int32_t prev2) {
#pragma HLS INLINE
    return 2 * prev1 - prev2;
}

// Adaptive Rice parameter for one window (spec 4.2.3):
//   k = max(0, floor(log2(S / count)))  over the sum of MAPPED residuals.
// `count` is the real coded residual count (window length minus the two anchors),
// which may be below the nominal 73 when consecutive beats truncate a window.
int ecg_select_k(uint64_t residual_sum, int count);

// Encode one QRS window losslessly, emitting through `out` (token stream or inline
// packer). Returns the chosen k.
int ecg_encode_qrs(const ecg_bram_word_t* samples, ecg_index_t start, int length,
                   ecg_coder_out& out);

// Module-level synthesis top: token-stream sink (used by module_lossless.tcl).
int ecg_encode_qrs_top(const ecg_bram_word_t* samples, ecg_index_t start, int length,
                       hls::stream<ecg_token>& tokens);

#endif  // ECG_LOSSLESS_H
