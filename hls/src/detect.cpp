// Adaptive QRS detection (spec 4.1). Bit-exact mirror of python/ecg_ref/detect.py.
#include "detect.h"

void ecg_detect_init(ecg_detect_state& s) {
    s.lp_acc = 0;
    s.hp_acc = 0;
    for (int i = 0; i < 3; i++) s.taps[i] = 0;
    for (int i = 0; i < ECG_MA_LEN; i++) s.ma_buf[i] = 0;
    s.ma_index = 0;
    s.ma_sum = 0;
    s.thr_acc = ECG_THRESH_INIT;
    s.refractory_left = 0;
    s.index = 0;
}

ecg_filt_t ecg_iir_step(ecg_detect_state& s, ecg_sample_t sample) {
#pragma HLS INLINE
    // Both accumulators carry ECG_IIR_FRAC_BITS extra fractional bits so the
    // arithmetic shift can never round the increment away to zero and stall the
    // filter on small inputs.
    const ecg_filt_t target = (ecg_filt_t)sample << ECG_IIR_FRAC_BITS;
    s.lp_acc += (ecg_filt_t)ecg_asr(target - s.lp_acc, ECG_IIR_LP_SHIFT);
    const ecg_filt_t lp_out = (ecg_filt_t)ecg_asr(s.lp_acc, ECG_IIR_FRAC_BITS);

    const ecg_filt_t hp_target = lp_out << ECG_IIR_FRAC_BITS;
    s.hp_acc += (ecg_filt_t)ecg_asr(hp_target - s.hp_acc, ECG_IIR_HP_SHIFT);
    return lp_out - (ecg_filt_t)ecg_asr(s.hp_acc, ECG_IIR_FRAC_BITS);
}

bool ecg_energy_step(ecg_detect_state& s, ecg_energy_t psi,
                     ecg_energy_t& smooth, ecg_energy_t& threshold) {
#pragma HLS INLINE
    // ---- Rectangular moving average, zero-initialised history ----
    s.ma_sum -= s.ma_buf[s.ma_index];
    s.ma_buf[s.ma_index] = psi;
    s.ma_sum += psi;
    s.ma_index = (s.ma_index + 1 == ECG_MA_LEN) ? 0 : (s.ma_index + 1);
    // ECG_MA_LEN is 18: use reciprocal multiply, not a general 64-bit divider.
    const ecg_energy_t sm = (ecg_energy_t)ecg_floor_div18(s.ma_sum);

    // ---- Adaptive threshold: absorbs the current sample before comparison ----
    const int64_t thr_target = (int64_t)sm << ECG_THRESH_FRAC_BITS;
    s.thr_acc += ecg_asr(thr_target - s.thr_acc, ECG_THRESH_SHIFT);
    const ecg_energy_t thr =
        (ecg_energy_t)ecg_asr(s.thr_acc, ECG_THRESH_FRAC_BITS);

    smooth = sm;
    threshold = thr;

    bool trigger = false;
    if (s.refractory_left > 0) {
        s.refractory_left--;
    } else if (s.index >= ECG_THRESH_WARMUP &&
               sm > (ecg_energy_t)ECG_THRESH_FACTOR * thr) {
        trigger = true;
        s.refractory_left = ECG_REFRACTORY;
    }

    s.index++;
    return trigger;
}

ecg_index_t ecg_search_peak(const ecg_bram_word_t* samples, ecg_index_t n_total,
                            ecg_index_t trigger) {
    ecg_index_t lo = trigger - ECG_PEAK_SEARCH_BACK;
    if (lo < 0) lo = 0;
    ecg_index_t hi = trigger + ECG_PEAK_SEARCH_FWD;
    if (hi > n_total - 1) hi = n_total - 1;

    ecg_index_t best_idx = lo;
    int32_t best_mag = -1;
search_peak:
    for (ecg_index_t m = lo; m <= hi; m++) {
#pragma HLS LOOP_TRIPCOUNT min = 1 max = 51
        const int32_t value = (int32_t)ecg_bram_read(samples, m);
        const int32_t mag = (value < 0) ? -value : value;
        if (mag > best_mag) {          // strict '>' keeps the earliest index on ties
            best_mag = mag;
            best_idx = m;
        }
    }
    return best_idx;
}

int ecg_detect_triggers(const ecg_bram_word_t* samples, ecg_index_t n_total,
                        ecg_index_t* triggers, int max_triggers,
                        ecg_filt_t* bandpass_trace, ecg_energy_t* tkeo_trace,
                        ecg_energy_t* smooth_trace, ecg_energy_t* threshold_trace) {
    ecg_detect_state s;
    ecg_detect_init(s);
    // 18 x 64-bit window must not consume block RAM; the sample store already owns
    // most of the XC7Z010 BRAM budget.
#pragma HLS BIND_STORAGE variable = s.ma_buf type = ram_1p impl = lutram

    int count = 0;
detect_stream:
    for (ecg_index_t n = 0; n <= n_total; n++) {
#pragma HLS LOOP_TRIPCOUNT min = 2 max = (ECG_N_SAMPLES + 1)
    // II=2: XC7Z010 is LUT/control-set bound; realtime headroom is >30000x so
    // a slightly slower detect pass is free area (Place 30-487).
#pragma HLS PIPELINE II = 2
        // Shift a new bandpass sample into the TKEO window. The extra iteration past
        // the end of the record only exists to score the final index, whose energy is
        // zero by definition, so the value shifted in there is irrelevant.
        ecg_filt_t bp = 0;
        if (n < n_total) {
            bp = ecg_iir_step(s, ecg_bram_read(samples, n));
            if (bandpass_trace) bandpass_trace[n] = bp;
        }
        s.taps[0] = s.taps[1];
        s.taps[1] = s.taps[2];
        s.taps[2] = bp;

        const ecg_index_t idx = n - 1;
        if (idx < 0) continue;

        // Boundary indices have no complete neighbour pair; their energy is zero.
        ecg_energy_t psi = 0;
        if (idx > 0 && idx < n_total - 1) {
            const ecg_energy_t centre = (ecg_energy_t)s.taps[1];
            psi = centre * centre -
                  (ecg_energy_t)s.taps[0] * (ecg_energy_t)s.taps[2];
        }

        ecg_energy_t sm, thr;
        const bool trigger = ecg_energy_step(s, psi, sm, thr);

        if (tkeo_trace) tkeo_trace[idx] = psi;
        if (smooth_trace) smooth_trace[idx] = sm;
        if (threshold_trace) threshold_trace[idx] = thr;

        if (trigger && count < max_triggers) {
            triggers[count++] = idx;
        }
    }
    return count;
}

int ecg_detect_pass(const ecg_bram_word_t* samples, ecg_index_t n_total,
                    ecg_index_t* peaks, int max_peaks) {
    const int count = ecg_detect_triggers(samples, n_total, peaks, max_peaks,
                                          0, 0, 0, 0);
    // Refine each trigger into an apex in place; the search window is far shorter
    // than the refractory period, so peaks stay strictly increasing.
refine_peaks:
    for (int i = 0; i < count; i++) {
#pragma HLS LOOP_TRIPCOUNT min = 1 max = ECG_MAX_PEAKS
        peaks[i] = ecg_search_peak(samples, n_total, peaks[i]);
    }
    return count;
}

int ecg_frame_windows(const ecg_index_t* peaks, int peak_count, ecg_index_t n_total,
                      ecg_index_t* starts, ecg_index_t* ends, int max_windows) {
    int count = 0;
    ecg_index_t prev_end = -1;

frame_windows:
    for (int i = 0; i < peak_count; i++) {
#pragma HLS LOOP_TRIPCOUNT min = 1 max = ECG_MAX_PEAKS
        const ecg_index_t r = peaks[i];

        // The refractory period is one sample shorter than the window, so adjacent
        // windows can collide; the later window is truncated rather than allowed to
        // overlap. This is the spec's Module 1 truncation requirement.
        ecg_index_t start = r - ECG_QRS_PRE;
        if (start < prev_end + 1) start = prev_end + 1;
        if (start < 0) start = 0;

        ecg_index_t end = r + ECG_QRS_POST;
        if (end > n_total - 1) end = n_total - 1;

        if (end < start) continue;
        if (count >= max_windows) break;

        starts[count] = start;
        ends[count] = end;
        count++;
        prev_end = end;
    }
    return count;
}
