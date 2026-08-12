#ifndef ECG_DETECT_H
#define ECG_DETECT_H

#include "ecg_types.h"

// Running state of the detection chain. Held explicitly rather than in statics so the
// testbenches can drive the chain sample by sample and inspect every intermediate.
struct ecg_detect_state {
    ecg_filt_t lp_acc;      // low-pass EMA accumulator (IIR_FRAC_BITS fractional)
    ecg_filt_t hp_acc;      // high-pass EMA accumulator
    ecg_filt_t taps[3];     // bandpass delay line feeding the TKEO window
    ecg_energy_t ma_buf[ECG_MA_LEN];
    int ma_index;
    ecg_energy_t ma_sum;
    int64_t thr_acc;        // threshold accumulator (THRESH_FRAC_BITS fractional)
    int refractory_left;
    ecg_index_t index;      // index of the next sample the energy stage will score
};

void ecg_detect_init(ecg_detect_state& s);

// First-order IIR bandpass, one sample. Two shift-based EMAs, no multiplier.
ecg_filt_t ecg_iir_step(ecg_detect_state& s, ecg_sample_t sample);

// Energy stage for one sample index: moving average, adaptive threshold, refractory
// gating. `psi` is the TKEO value for the index being scored. Returns true on trigger.
bool ecg_energy_step(ecg_detect_state& s, ecg_energy_t psi,
                     ecg_energy_t& smooth, ecg_energy_t& threshold);

// Locate the R apex around a trigger by scanning for the largest |sample|.
// Searches both directions: the trigger may lag or lead the apex.
ecg_index_t ecg_search_peak(const ecg_bram_word_t* samples, ecg_index_t n_total,
                            ecg_index_t trigger);

// Streaming detection pass over a BRAM-resident record.
//
// The TKEO at index i needs bp[i-1], bp[i] and bp[i+1], so index i can only be scored
// once sample i+1 has been read; the loop therefore runs one iteration past the record
// and scores index (n-1) on iteration n. Indices 0 and n_total-1 have no complete
// neighbour pair and are defined to have zero energy, matching the zero-initialised
// delay line in the reference model.
//
// Traces are optional; pass NULL to skip recording them (they are only needed for
// verification, since storing them for a whole record would not fit on chip).
int ecg_detect_triggers(const ecg_bram_word_t* samples, ecg_index_t n_total,
                        ecg_index_t* triggers, int max_triggers,
                        ecg_filt_t* bandpass_trace, ecg_energy_t* tkeo_trace,
                        ecg_energy_t* smooth_trace, ecg_energy_t* threshold_trace);

// Full detection: triggers followed by apex refinement.
int ecg_detect_pass(const ecg_bram_word_t* samples, ecg_index_t n_total,
                    ecg_index_t* peaks, int max_peaks);

// Turn R-peak indices into non-overlapping [start, end] QRS windows.
// Returns the window count; windows may be shorter than ECG_QRS_WINDOW when
// consecutive beats collide.
int ecg_frame_windows(const ecg_index_t* peaks, int peak_count, ecg_index_t n_total,
                      ecg_index_t* starts, ecg_index_t* ends, int max_windows);

#endif  // ECG_DETECT_H
