// Lossless QRS path (spec 4.2). Bit-exact mirror of python/ecg_ref/lossless.py.
#include "lossless.h"

int ecg_select_k(uint64_t residual_sum, int count) {
    if (count <= 0) return 0;
    const uint64_t mean = residual_sum / (uint64_t)count;
    if (mean == 0) return 0;                     // avoids log2(0)
    const int k = ecg_floor_log2(mean);
    return (k > ECG_GR_K_MAX) ? ECG_GR_K_MAX : k;
}

int ecg_encode_qrs(const ecg_bram_word_t* samples, ecg_index_t start, int length,
                   ecg_coder_out& out) {
    // Inlined into ecg_encode_stage at a single call site (shared emit/bitpack).
#pragma HLS INLINE
    const int32_t anchor0 = (length > 0) ? (int32_t)ecg_bram_read(samples, start) : 0;
    const int32_t anchor1 =
        (length > 1) ? (int32_t)ecg_bram_read(samples, start + 1) : 0;

    // ---- Pass 1: accumulate the mapped residual sum so k can be chosen ----
    // k must be known before any residual is emitted because it is transmitted in the
    // packet header, so the window is walked twice. The window is at most 73 samples,
    // so this costs a negligible amount of time and no extra storage.
    uint64_t residual_sum = 0;
    int coded = 0;
k_scan:
    for (int i = ECG_ANCHOR_COUNT; i < length; i++) {
#pragma HLS LOOP_TRIPCOUNT min = 0 max = ECG_QRS_WINDOW
        const int32_t prev1 = (int32_t)ecg_bram_read(samples, start + i - 1);
        const int32_t prev2 = (int32_t)ecg_bram_read(samples, start + i - 2);
        const int32_t error =
            (int32_t)ecg_bram_read(samples, start + i) - ecg_predict(prev1, prev2);
        residual_sum += (uint64_t)ecg_zigzag(error);
        coded++;
    }
    const int k = ecg_select_k(residual_sum, coded);

    // ---- Packet header ----
    ecg_emit_bits(out, ECG_TAG_QRS, 1);
    ecg_emit_bits(out, (uint32_t)length, ECG_LEN_FIELD_BITS);
    ecg_emit_bits(out, (uint32_t)anchor0 & 0xFFFFu, 16);
    ecg_emit_bits(out, (uint32_t)anchor1 & 0xFFFFu, 16);
    ecg_emit_bits(out, (uint32_t)k, ECG_GR_K_BITS);

    // ---- Pass 2: emit the Golomb-Rice codewords ----
    const uint32_t remainder_mask =
        (k > 0) ? ((1u << k) - 1u) : 0u;
gr_encode:
    for (int i = ECG_ANCHOR_COUNT; i < length; i++) {
#pragma HLS LOOP_TRIPCOUNT min = 0 max = ECG_QRS_WINDOW
        const int32_t prev1 = (int32_t)ecg_bram_read(samples, start + i - 1);
        const int32_t prev2 = (int32_t)ecg_bram_read(samples, start + i - 2);
        const int32_t error =
            (int32_t)ecg_bram_read(samples, start + i) - ecg_predict(prev1, prev2);
        const uint32_t mapped = ecg_zigzag(error);
        const uint32_t quotient = mapped >> k;

        if (quotient >= (uint32_t)ECG_GR_Q_ESCAPE) {
            // Escape: a fixed run of ones with NO terminating zero, then the raw
            // value. Without this an outlier residual would emit a unary quotient
            // thousands of bits long; the escape caps any symbol at 63 bits.
            ecg_emit_bits(out, 0x7FFFFFFFu, ECG_GR_Q_ESCAPE);
            ecg_emit_bits(out, mapped, ECG_GR_ESCAPE_BITS);
        } else {
            // `quotient` ones followed by a terminating zero, then k remainder bits.
            const uint32_t unary = ((1u << quotient) - 1u) << 1;
            ecg_emit_bits(out, unary, (int)quotient + 1);
            if (k > 0) ecg_emit_bits(out, mapped & remainder_mask, k);
        }
    }

    ecg_emit_align(out);
    return k;
}

int ecg_encode_qrs_top(const ecg_bram_word_t* samples, ecg_index_t start, int length,
                       hls::stream<ecg_token>& tokens) {
    ecg_coder_out out = ecg_coder_tokens(tokens);
    return ecg_encode_qrs(samples, start, length, out);
}
