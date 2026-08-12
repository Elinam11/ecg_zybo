#ifndef ECG_TOP_H
#define ECG_TOP_H

#include "bitpack.h"
#include "detect.h"
#include "ecg_types.h"
#include "lossless.h"
#include "lossy.h"

// Top-level compression core. `ecg_status` is defined in ecg_types.h, because the
// packer stage is what populates it.
//
// The core streams ONLY the segment-packet payload. The 128-bit frame header is
// assembled by the PS from these status registers, because word 3 of the header is a
// checksum over the payload and a streaming master cannot go back and patch a header
// it has already transmitted. Buffering the whole payload on chip to avoid that is not
// an option: the sample store alone already occupies 78% of device BRAM.
//
// ecg_build_header() below is the single definition of that assembly, shared by the
// testbenches, the PS firmware and the Python decoder, so the three cannot disagree.
// The final payload word carries last = true, which becomes TLAST on the AXI4-Stream
// and terminates the DMA transfer.
void ecg_top(const ecg_bram_word_t* ecg_bram, uint32_t n_samples,
             hls::stream<ecg_stream_word>& out_stream, ecg_status& status);

// Fill `words[0..3]` with the frame header for a completed run.
void ecg_build_header(const ecg_status& status, uint32_t n_samples,
                      ecg_word_t* words);

#endif  // ECG_TOP_H
