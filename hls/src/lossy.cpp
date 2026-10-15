// bior4.4 (CDF 9/7) lossy path. Bit-exact with python/ecg_ref/lossy_bw.py.
#include "lossy.h"

// One fold is enough: lifting only indexes i±1 of a length-n buffer.
static int ecg_reflect_idx(int i, int n) {
#pragma HLS INLINE
    if (n <= 1) return 0;
    if (i < 0) return -i;
    if (i > n - 1) return 2 * (n - 1) - i;
    return i;
}

int ecg_bw_l_seg(int n) {
    if (n < 2) return 0;
    int l = 0;
    int size = n;
    while (l < ECG_BW_L_MAX && size >= 2) {
        const int next_approx = (size + 1) >> 1;
        if (next_approx < ECG_BW_MIN_APPROX) break;
        size = next_approx;
        l++;
        if (size < 2) break;
    }
    return l;
}

static void ecg_rice_k(int is_approx, int band_level, int* k_run, int* k_lvl) {
#pragma HLS INLINE
    if (is_approx) {
        static const int kr[6] = {0, 0, 0, 0, 0, 0};
        static const int kl[6] = {4, 6, 6, 6, 6, 7};
        const int i = (band_level < 0) ? 0 : (band_level > 5 ? 5 : band_level);
        *k_run = kr[i];
        *k_lvl = kl[i];
    } else {
        // cD2..cD5
        int kr = 0, kl = 1;
        if (band_level == 2) { kr = 1; kl = 0; }
        else if (band_level == 3) { kr = 0; kl = 2; }
        else if (band_level == 4) { kr = 0; kl = 3; }
        else if (band_level == 5) { kr = 0; kl = 5; }
        *k_run = kr;
        *k_lvl = kl;
    }
}

static int ecg_rice_nbits(uint32_t value, int k) {
#pragma HLS INLINE off
    if (k < 0) k = 0;
    if (k > ECG_GR_K_MAX) k = ECG_GR_K_MAX;
    uint32_t q = value;
    for (int i = 0; i < k; i++) {
#pragma HLS PIPELINE off
#pragma HLS UNROLL off
#pragma HLS LOOP_TRIPCOUNT min = 0 max = 15
        q >>= 1;
    }
    if (q >= (uint32_t)ECG_BW_RICE_Q_ESC) return ECG_BW_RICE_Q_ESC + ECG_BW_RICE_ESC_BITS;
    return (int)q + 1 + k;
}

static void ecg_rice_emit(ecg_coder_out& out, uint32_t value, int k) {
#pragma HLS INLINE
    ecg_emit_golomb_rice(out, value, k);
}

// Delta is exactly 1 (a = 1, T = 1 ADU). The general Q0.16 reciprocal multiply
// then collapses to round(mag / 64) on the Q6 coefficient: (mag + 32) >> 6.
static int32_t ecg_dzq(int32_t c, int32_t t_q6, uint32_t inv_d) {
#pragma HLS INLINE off
    (void)inv_d;
    const int32_t mag = (c < 0) ? -c : c;
    if (mag < t_q6) return 0;
    int32_t q = (int32_t)(((uint32_t)mag + 32u) >> 6);
    if (q < 1) q = 1;
    return (c >= 0) ? q : -q;
}

// LeGall 5/3 integer lifting. Matches lossy_bw._lift_level_53_int: odds then evens,
// floor division via arithmetic shift, then even/odd split. No Q16 multipliers.
static void ecg_lift53_level(ecg_coeff_t* buf, int n, ecg_coeff_t* scratch) {
#pragma HLS INLINE off
    if (n < 2) return;
    for (int i = 0; i < n; i++) {
#pragma HLS PIPELINE off
#pragma HLS LOOP_TRIPCOUNT min = 2 max = ECG_MAX_LOSSY_SEGMENT
        scratch[i] = buf[i];
    }
    for (int i = 1; i < n; i += 2) {
#pragma HLS PIPELINE off
#pragma HLS LOOP_TRIPCOUNT min = 1 max = (ECG_MAX_LOSSY_SEGMENT / 2)
        const int64_t neigh = (int64_t)scratch[ecg_reflect_idx(i - 1, n)] +
                              (int64_t)scratch[ecg_reflect_idx(i + 1, n)];
        scratch[i] = (ecg_coeff_t)((int64_t)scratch[i] - (neigh >> 1));
    }
    for (int i = 0; i < n; i += 2) {
#pragma HLS PIPELINE off
#pragma HLS LOOP_TRIPCOUNT min = 1 max = (ECG_MAX_LOSSY_SEGMENT / 2)
        const int64_t neigh = (int64_t)scratch[ecg_reflect_idx(i - 1, n)] +
                              (int64_t)scratch[ecg_reflect_idx(i + 1, n)];
        scratch[i] = (ecg_coeff_t)((int64_t)scratch[i] + ((neigh + 2) >> 2));
    }
    const int n_low = (n + 1) >> 1;
    const int n_high = n >> 1;
    for (int i = 0; i < n_low; i++) {
#pragma HLS PIPELINE off
#pragma HLS LOOP_TRIPCOUNT min = 1 max = (ECG_MAX_LOSSY_SEGMENT / 2)
        buf[i] = scratch[2 * i];
    }
    for (int i = 0; i < n_high; i++) {
#pragma HLS PIPELINE off
#pragma HLS LOOP_TRIPCOUNT min = 1 max = (ECG_MAX_LOSSY_SEGMENT / 2)
        buf[n_low + i] = scratch[2 * i + 1];
    }
}

static int ecg_sample_q6(const ecg_bram_word_t* samples, ecg_index_t i) {
#pragma HLS INLINE
    return ((int32_t)ecg_bram_read(samples, i)) << ECG_BW_FRAC_DATA;
}

static int ecg_ca_dpcm_rice_bits(const int32_t* vals, int n, int band_level) {
#pragma HLS INLINE off
    int k_run, k_lvl;
    ecg_rice_k(1, band_level, &k_run, &k_lvl);
    (void)k_run;
    int bits = ecg_rice_nbits((uint32_t)n, ECG_BW_RICE_K_NTOK);
    int32_t prev = 0;
    int32_t loaded = 0;
    bool have = false;
    for (int i = 0; i < n; i++) {
#pragma HLS PIPELINE off
        if (have) {
            const int32_t d = loaded - prev;
            prev = loaded;
            bits += ecg_rice_nbits(ecg_zigzag(d), k_lvl);
        }
        loaded = vals[i];
        have = true;
    }
    if (have) bits += ecg_rice_nbits(ecg_zigzag(loaded - prev), k_lvl);
    return bits;
}

static void ecg_ca_dpcm_rice_emit(ecg_coder_out& out, const int32_t* vals, int n,
                                 int band_level) {
#pragma HLS INLINE
    int k_run, k_lvl;
    ecg_rice_k(1, band_level, &k_run, &k_lvl);
    (void)k_run;
    ecg_rice_emit(out, (uint32_t)n, ECG_BW_RICE_K_NTOK);
    int32_t prev = 0;
    int32_t loaded = 0;
    bool have = false;
    for (int i = 0; i < n; i++) {
        // PIPELINE off: a pipelined emit loop replicates ecg_bitpack_put.
        // The sample is consumed on the next iteration so the BRAM load is not
        // in series with the Rice shift (that chain was 9.69 ns).
#pragma HLS PIPELINE off
        if (have) {
            const int32_t d = loaded - prev;
            prev = loaded;
            ecg_rice_emit(out, ecg_zigzag(d), k_lvl);
        }
        loaded = vals[i];
        have = true;
    }
    if (have) {
        ecg_rice_emit(out, ecg_zigzag(loaded - prev), k_lvl);
    }
}

static int ecg_rle_rice_bits(const int32_t* vals, int n, int is_approx, int band_level) {
#pragma HLS INLINE off
    int k_run, k_lvl;
    ecg_rice_k(is_approx, band_level, &k_run, &k_lvl);
    int ntok = 0;
    int run = 0;
    int bits = 0;
    for (int i = 0; i < n; i++) {
#pragma HLS PIPELINE off
        const int32_t v = vals[i];
        if (v == 0) run++;
        else {
            ntok++;
            const int nb_run = ecg_rice_nbits((uint32_t)run, k_run);
            const uint32_t zz = ecg_hold_u32(ecg_zigzag(v));
            bits += nb_run;
            bits += ecg_rice_nbits(zz, k_lvl);
            run = 0;
        }
    }
    bits += ecg_rice_nbits((uint32_t)ntok, ECG_BW_RICE_K_NTOK);
    return bits;
}

static void ecg_rle_rice_emit(ecg_coder_out& out, const int32_t* vals, int n,
                              int is_approx, int band_level) {
#pragma HLS INLINE
    int k_run, k_lvl;
    ecg_rice_k(is_approx, band_level, &k_run, &k_lvl);
    int ntok = 0;
    for (int i = 0; i < n; i++) {
#pragma HLS PIPELINE off
        if (vals[i] != 0) ntok++;
    }
    ecg_rice_emit(out, (uint32_t)ntok, ECG_BW_RICE_K_NTOK);
    int run = 0;
    for (int i = 0; i < n; i++) {
        // PIPELINE off: keep one shared ecg_bitpack_put instance (see above).
#pragma HLS PIPELINE off
        if (vals[i] == 0) run++;
        else {
            ecg_rice_emit(out, (uint32_t)run, k_run);
            ecg_rice_emit(out, ecg_zigzag(vals[i]), k_lvl);
            run = 0;
        }
    }
}

int ecg_encode_nonqrs(const ecg_bram_word_t* samples, ecg_index_t start, int length,
                      ecg_coeff_t* buf, ecg_coeff_t* scratch, ecg_coder_out& out,
                      int* saturated,
                      ecg_index_t left_start, int left_len,
                      ecg_index_t right_start, int right_len) {
#pragma HLS INLINE
    if (saturated) *saturated = 0;
    (void)left_start;
    (void)left_len;
    (void)right_start;
    (void)right_len;
    const int n = length;
    const int levels = ecg_bw_l_seg(n);
    const int n_ext = n;

    for (int i = 0; i < n; i++) {
#pragma HLS PIPELINE off
#pragma HLS LOOP_TRIPCOUNT min = 1 max = ECG_MAX_LOSSY_SEGMENT
        buf[i] = ecg_sample_q6(samples, start + i);
    }

    int len = n_ext;
    for (int lv = 0; lv < levels; lv++) {
        ecg_lift53_level(buf, len, scratch);
        len = (len + 1) >> 1;
    }

    // Packed layout after L levels: [cA_L | cD_L | ... | cD1]
    // Band offsets from n_ext.
    int band_off[8];
    int band_len[8];
    int acc = 0;
    // We will fill from the front after computing lengths backwards...
    int lens_high[6];
    int n_ca = n_ext;
    for (int j = 1; j <= levels; j++) {
        const int n_low = (n_ca + 1) >> 1;
        const int n_high = n_ca >> 1;
        lens_high[j] = n_high;
        n_ca = n_low;
    }
    band_off[0] = 0;
    band_len[0] = (levels == 0) ? n_ext : n_ca;  // cA
    acc = band_len[0];
    for (int j = levels; j >= 1; j--) {
        band_off[j] = acc;  // cD_j
        band_len[j] = lens_high[j];
        acc += lens_high[j];
    }

    // Extract inside quantized coeffs into scratch, per band, coding order.
    // scratch layout: concatenated coded bands (cA then cD_L .. cD2)
    int32_t* coded = scratch;
    int coded_off[8];
    int coded_len[8];
    int cpos = 0;
    const int t_q6 = ECG_BW_DZ_T_Q6;
    const uint32_t inv_d = (uint32_t)ECG_BW_DZ_INV_D_Q16;

    if (levels == 0) {
        coded_off[0] = 0;
        for (int i = 0; i < n; i++) {
#pragma HLS PIPELINE off
            coded[i] = ecg_dzq(buf[i], t_q6, inv_d);
        }
        coded_len[0] = n;
        cpos = n;
    } else {
        // method a: keep all except cD1
        coded_off[0] = 0;
        for (int i = 0; i < band_len[0]; i++) {
#pragma HLS PIPELINE off
            coded[i] = ecg_dzq(buf[band_off[0] + i], t_q6, inv_d);
        }
        coded_len[0] = band_len[0];
        cpos = band_len[0];
        for (int j = levels; j >= 2; j--) {
            coded_off[j] = cpos;
            for (int i = 0; i < band_len[j]; i++) {
#pragma HLS PIPELINE off
                coded[cpos + i] = ecg_dzq(buf[band_off[j] + i], t_q6, inv_d);
            }
            coded_len[j] = band_len[j];
            cpos += band_len[j];
        }
    }

    // Count Rice bits first, then emit header + payload. A capture BRAM for the
    // word-count field was 8 of the 7 BRAM36 left after the sample store.
    int rice_bits = 0;
    if (levels == 0) {
        rice_bits += ecg_ca_dpcm_rice_bits(coded, coded_len[0], 0);
    } else {
        rice_bits += ecg_ca_dpcm_rice_bits(coded + coded_off[0], coded_len[0], levels);
        for (uint8_t j = (uint8_t)levels; j >= 2; j--)
            rice_bits += ecg_rle_rice_bits(coded + coded_off[j], coded_len[j], 0, (int)j);
    }
    const int rice_pad = (32 - (rice_bits % 32)) % 32;
    const int word_count = (rice_bits + rice_pad) / 32;
    ecg_emit_bits(out, ECG_TAG_NONQRS, 1);
    ecg_emit_bits(out, (uint32_t)n, ECG_LEN_FIELD_BITS);
    ecg_emit_bits(out, (uint32_t)word_count, ECG_SYMCOUNT_BITS);
    ecg_emit_align(out);
    if (levels == 0) {
        ecg_ca_dpcm_rice_emit(out, coded, coded_len[0], 0);
    } else {
        ecg_ca_dpcm_rice_emit(out, coded + coded_off[0], coded_len[0], levels);
        for (uint8_t j = (uint8_t)levels; j >= 2; j--)
            ecg_rle_rice_emit(out, coded + coded_off[j], coded_len[j], 0, (int)j);
    }
    ecg_emit_align(out);
    return word_count;
}

int ecg_encode_nonqrs_top(const ecg_bram_word_t* samples, ecg_index_t start, int length,
                          ecg_coeff_t* buf, ecg_coeff_t* scratch,
                          hls::stream<ecg_token>& tokens, int* saturated) {
    ecg_coder_out out = ecg_coder_tokens(tokens);
    return ecg_encode_nonqrs(samples, start, length, buf, scratch, out, saturated,
                             0, 0, 0, 0);
}
