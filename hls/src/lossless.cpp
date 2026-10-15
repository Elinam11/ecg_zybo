// Lossless QRS path (spec 4.2). Bit-exact mirror of python/ecg_ref/lossless.py.
#include "lossless.h"

int ecg_select_k(uint64_t residual_sum, int count) {
    if (count <= 0) return 0;
    // floor(log2(sum / count)), capped at GR_K_MAX. Same result as dividing then
    // taking floor_log2, without a general divider (~300 LUT on the XC7Z010).
    if (residual_sum < (uint64_t)count) return 0;
    int k = 0;
    uint64_t thresh = (uint64_t)count;
    while (k < ECG_GR_K_MAX) {
#pragma HLS PIPELINE off
#pragma HLS UNROLL off
#pragma HLS LOOP_TRIPCOUNT min = 0 max = 15
        const uint64_t next = thresh << 1;
        if (residual_sum < next) break;
        thresh = next;
        k++;
    }
    return k;
}

int ecg_encode_qrs(const ecg_bram_word_t* samples, ecg_index_t start, int length,
                   ecg_coder_out& out) {
    // Inlined into ecg_encode_stage at a single call site (shared emit/bitpack).
#pragma HLS INLINE
    // Copy the window through a single sequential BRAM port. Csynth otherwise
    // auto-pipelines k_scan onto a dual-port ecg_bram (PORTA+PORTB). The BD only
    // wires PORTA to the sample store, so PORTB reads are unbound and the encoder
    // sees garbage (record 100: anchors 8,-11 and k=7 instead of -11,-11 and k=2).
    ecg_sample_t window[ECG_QRS_WINDOW];
#pragma HLS BIND_STORAGE variable = window type = ram_1p impl = lutram
    if (length > ECG_QRS_WINDOW) length = ECG_QRS_WINDOW;
    // One 32-bit BRAM load writes both packed samples. Pipelining that loop
    // (even II=2) still captured the previous word, so the whole window was
    // orig[start-2:] (record 100: k=2, CR~9.58, PRD QRS ~55%).
load_qrs:
    {
        // 8-bit index. A 32-bit compare here was chained with the BRAM load
        // into a 9.6 ns path (encode_stage slack -1.19). The window is 73 samples.
        const uint8_t nwin = (uint8_t)length;
        uint8_t i = 0;
        if ((start & 1) && nwin > 0) {
            window[0] = ecg_bram_read(samples, start);
            i = 1;
        }
        for (; (uint8_t)(i + 1u) < nwin; i = (uint8_t)(i + 2u)) {
#pragma HLS LOOP_TRIPCOUNT min = 0 max = (ECG_QRS_WINDOW / 2)
#pragma HLS PIPELINE off
            const ecg_index_t word_idx = (start + (ecg_index_t)i) >> 1;
            const uint32_t word = ecg_bram_load_word(samples, word_idx);
            window[i] = (ecg_sample_t)(uint16_t)(word & 0xFFFFu);
            window[i + 1] = (ecg_sample_t)(uint16_t)(word >> 16);
        }
        if (i < nwin) {
            window[i] = ecg_bram_read(samples, start + (ecg_index_t)i);
        }
    }

    const int32_t anchor0 = (length > 0) ? (int32_t)window[0] : 0;
    const int32_t anchor1 = (length > 1) ? (int32_t)window[1] : 0;

    // ---- Pass 1: accumulate the mapped residual sum so k can be chosen ----
    // k must be known before any residual is emitted because it is transmitted in the
    // packet header, so the window is walked twice. The window is at most 73 samples,
    // so this costs a negligible amount of time and no extra storage.
    uint64_t residual_sum = 0;
    int coded = 0;
k_scan:
    for (int i = ECG_ANCHOR_COUNT; i < length; i++) {
#pragma HLS LOOP_TRIPCOUNT min = 0 max = ECG_QRS_WINDOW
#pragma HLS PIPELINE off
        const int32_t prev1 = (int32_t)window[i - 1];
        const int32_t prev2 = (int32_t)window[i - 2];
        const int32_t error = (int32_t)window[i] - ecg_predict(prev1, prev2);
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
gr_encode:
    for (int i = ECG_ANCHOR_COUNT; i < length; i++) {
#pragma HLS LOOP_TRIPCOUNT min = 0 max = ECG_QRS_WINDOW
#pragma HLS PIPELINE off
        const int32_t prev1 = (int32_t)window[i - 1];
        const int32_t prev2 = (int32_t)window[i - 2];
        const int32_t error = (int32_t)window[i] - ecg_predict(prev1, prev2);
        ecg_emit_golomb_rice(out, ecg_zigzag(error), k);
    }

    ecg_emit_align(out);
    return k;
}

int ecg_encode_qrs_top(const ecg_bram_word_t* samples, ecg_index_t start, int length,
                       hls::stream<ecg_token>& tokens) {
    ecg_coder_out out = ecg_coder_tokens(tokens);
    return ecg_encode_qrs(samples, start, length, out);
}
