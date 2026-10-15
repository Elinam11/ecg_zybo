#ifndef ECG_LOSSY_H
#define ECG_LOSSY_H

#include "bitpack.h"
#include "ecg_types.h"

int ecg_bw_l_seg(int n);

// bior4.4 lifting + DZQ + method-(a) mirror. cA is DPCM + per-sample Rice;
// detail bands (cD2..) keep zero-run RLE + Rice. Neighbour arguments are
// unused (kept so the testbench / top signature stays stable).
int ecg_encode_nonqrs(const ecg_bram_word_t* samples, ecg_index_t start, int length,
                      ecg_coeff_t* buf, ecg_coeff_t* scratch, ecg_coder_out& out,
                      int* saturated,
                      ecg_index_t left_start, int left_len,
                      ecg_index_t right_start, int right_len);

int ecg_encode_nonqrs_top(const ecg_bram_word_t* samples, ecg_index_t start, int length,
                          ecg_coeff_t* buf, ecg_coeff_t* scratch,
                          hls::stream<ecg_token>& tokens, int* saturated);

#endif  // ECG_LOSSY_H
