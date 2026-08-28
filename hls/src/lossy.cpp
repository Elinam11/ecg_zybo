// Lossy non-QRS path (spec 4.3). Bit-exact mirror of python/ecg_ref/lossy.py.
#include "lossy.h"

void ecg_haar_level(ecg_coeff_t* buf, int len, ecg_coeff_t* scratch) {
    const int half = len >> 1;
haar_pairs:
    for (int m = 0; m < half; m++) {
#pragma HLS LOOP_TRIPCOUNT min = 1 max = (ECG_MAX_LOSSY_SEGMENT / 2)
#pragma HLS PIPELINE II = 1
        const ecg_coeff_t even = buf[2 * m];
        const ecg_coeff_t odd = buf[2 * m + 1];
        // Reversible lifting Haar: no multiplier, and exactly invertible so the
        // transform itself contributes zero distortion.
        const ecg_coeff_t detail = even - odd;
        const ecg_coeff_t approx = odd + (ecg_coeff_t)ecg_asr(detail, 1);
        scratch[m] = approx;
        scratch[half + m] = detail;
    }
haar_copy:
    for (int i = 0; i < len; i++) {
#pragma HLS LOOP_TRIPCOUNT min = 1 max = ECG_MAX_LOSSY_SEGMENT
#pragma HLS PIPELINE II = 1
        buf[i] = scratch[i];
    }
}

void ecg_haar_forward(ecg_coeff_t* buf, int padded_len, ecg_coeff_t* scratch) {
    int len = padded_len;
haar_levels:
    for (int level = 0; level < ECG_DWT_LEVELS; level++) {
        ecg_haar_level(buf, len, scratch);
        len >>= 1;
    }
}

int ecg_quantize_region(ecg_coeff_t* buf, int offset, int count,
                        int threshold, int shift) {
    const ecg_coeff_t half = (shift > 0) ? (ecg_coeff_t)(1 << (shift - 1)) : 0;
    int saturated = 0;
quantize_region:
    for (int i = 0; i < count; i++) {
#pragma HLS LOOP_TRIPCOUNT min = 1 max = ECG_MAX_LOSSY_SEGMENT
#pragma HLS PIPELINE II = 1
        const ecg_coeff_t raw = buf[offset + i];
        const ecg_coeff_t mag = (raw < 0) ? -raw : raw;
        if (mag < (ecg_coeff_t)threshold) {
            buf[offset + i] = 0;
            continue;
        }
        // Round to nearest before the shift, then saturate into the 10-bit field.
        const ecg_coeff_t scaled = (ecg_coeff_t)ecg_asr(raw + half, shift);
        const ecg_coeff_t limited =
            (ecg_coeff_t)ecg_clamp(scaled, ECG_RLE_VAL_MIN, ECG_RLE_VAL_MAX);
        if (limited != scaled) saturated++;
        buf[offset + i] = limited;
    }
    return saturated;
}

int ecg_encode_nonqrs(const ecg_bram_word_t* samples, ecg_index_t start, int length,
                      ecg_coeff_t* buf, ecg_coeff_t* scratch, ecg_coder_out& out,
                      int* saturated) {
    // Inlined into ecg_encode_stage at a single call site (shared emit/bitpack).
#pragma HLS INLINE
    const int padded_len = ecg_dwt_padded_len(length);

    // Load packed words, then pad. Sample-by-sample II=1/2 skews the even half
    // the same way as the QRS window load (see lossless.cpp).
    int n = 0;
    if ((start & 1) && length > 0) {
        buf[0] = (ecg_coeff_t)ecg_bram_read(samples, start);
        n = 1;
    }
load_segment:
    for (; n + 1 < length; n += 2) {
#pragma HLS LOOP_TRIPCOUNT min = 0 max = (ECG_MAX_LOSSY_SEGMENT / 2)
#pragma HLS PIPELINE off
        const uint32_t word = ecg_bram_load_word(samples, (start + n) >> 1);
        buf[n] = (ecg_coeff_t)(ecg_sample_t)(uint16_t)(word & 0xFFFFu);
        buf[n + 1] = (ecg_coeff_t)(ecg_sample_t)(uint16_t)(word >> 16);
    }
    if (n < length) {
        buf[n] = (ecg_coeff_t)ecg_bram_read(samples, start + n);
        n++;
    }
pad_segment:
    for (; n < padded_len; n++) {
#pragma HLS LOOP_TRIPCOUNT min = 0 max = ECG_DWT_BLOCK
        buf[n] = buf[length - 1];
    }

    ecg_haar_forward(buf, padded_len, scratch);

    // Band regions, in the layout the in-place transform already produced.
    const int n_a4 = padded_len >> 4;
    const int n_d4 = padded_len >> 4;
    const int n_d3 = padded_len >> 3;
    const int n_d2 = padded_len >> 2;
    const int n_d1 = padded_len >> 1;

    int sat = 0;
    int offset = 0;
    sat += ecg_quantize_region(buf, offset, n_a4, ECG_T_A4, ECG_QS_A4);
    offset += n_a4;
    sat += ecg_quantize_region(buf, offset, n_d4, ECG_T_D4, ECG_QS_D4);
    offset += n_d4;
    sat += ecg_quantize_region(buf, offset, n_d3, ECG_T_D3, ECG_QS_D3);
    offset += n_d3;
    sat += ecg_quantize_region(buf, offset, n_d2, ECG_T_D2, ECG_QS_D2);
    offset += n_d2;
    sat += ecg_quantize_region(buf, offset, n_d1, ECG_T_D1, ECG_QS_D1);
    if (saturated) *saturated = sat;

    // ---- Run-length code, counting tokens before emitting the packet ----
    // The symbol count sits in the packet header ahead of the tokens, so the run
    // lengths are computed first. Only non-zero coefficients produce a token, which
    // is what makes a zero value field an unambiguous continuation marker.
    int token_count = 0;
    int run = 0;
count_tokens:
    for (int i = 0; i < padded_len; i++) {
#pragma HLS LOOP_TRIPCOUNT min = ECG_DWT_BLOCK max = ECG_MAX_LOSSY_SEGMENT
#pragma HLS PIPELINE II = 1
        if (buf[i] == 0) {
            run++;
            if (run == ECG_RLE_RUN_MAX) {
                token_count++;      // continuation marker
                run = 0;
            }
        } else {
            token_count++;
            run = 0;
        }
    }

    ecg_emit_bits(out, ECG_TAG_NONQRS, 1);
    ecg_emit_bits(out, (uint32_t)length, ECG_LEN_FIELD_BITS);
    ecg_emit_bits(out, (uint32_t)token_count, ECG_SYMCOUNT_BITS);

    run = 0;
emit_tokens:
    for (int i = 0; i < padded_len; i++) {
#pragma HLS LOOP_TRIPCOUNT min = ECG_DWT_BLOCK max = ECG_MAX_LOSSY_SEGMENT
        const ecg_coeff_t value = buf[i];
        if (value == 0) {
            run++;
            if (run == ECG_RLE_RUN_MAX) {
                const uint32_t code =
                    ((uint32_t)ECG_RLE_RUN_MAX << ECG_RLE_VAL_BITS);
                ecg_emit_bits(out, code, ECG_RLE_TOKEN_BITS);
                run = 0;
            }
        } else {
            const uint32_t code =
                (((uint32_t)run & ECG_RLE_RUN_MAX) << ECG_RLE_VAL_BITS) |
                ((uint32_t)value & ((1u << ECG_RLE_VAL_BITS) - 1u));
            ecg_emit_bits(out, code, ECG_RLE_TOKEN_BITS);
            run = 0;
        }
    }
    // Trailing zeros are never emitted: the decoder derives the coefficient count
    // from the segment length and pads the remainder itself.

    ecg_emit_align(out);
    return token_count;
}

int ecg_encode_nonqrs_top(const ecg_bram_word_t* samples, ecg_index_t start, int length,
                          ecg_coeff_t* buf, ecg_coeff_t* scratch,
                          hls::stream<ecg_token>& tokens, int* saturated) {
    ecg_coder_out out = ecg_coder_tokens(tokens);
    return ecg_encode_nonqrs(samples, start, length, buf, scratch, out, saturated);
}
