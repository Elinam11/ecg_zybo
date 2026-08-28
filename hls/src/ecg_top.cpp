// Top-level ECG compression core. Mirrors python/ecg_ref/pipeline.py exactly.
#include "ecg_top.h"

void ecg_build_header(const ecg_status& status, uint32_t n_samples,
                      ecg_word_t* words) {
    words[0] = ECG_MAGIC_WORD;
    words[1] = n_samples;
    words[2] = ((uint32_t)ECG_FS & 0xFFFFu) | ((status.qrs_count & 0xFFFFu) << 16);
    words[3] = status.checksum;
}

// Walk the record, route each region to the right coder, and pack bits onto the
// AXI4-Stream as they are produced (no token FIFO between concurrent processes).
//
// Inline packing keeps the XC7Z010 BRAM budget: the sample store already uses ~53 of
// 60 BRAM36. A deep BRAM token FIFO overruns the device; DATAFLOW + shallow FIFO +
// AXIS deadlocks in RTL cosim (packer waits on TREADY, TB waits on ap_done).
//
// Single call site each for QRS / non-QRS (both inlined): avoids HLS duplicating the
// lossy pipelines when non-QRS was inlined at gap+trailing sites (csynth rose to
// 17630 LUTs / 100%). emit/bitpack stay INLINE off so one packer body is shared.
static void ecg_encode_stage(const ecg_bram_word_t* ecg_bram, ecg_index_t total,
                             const ecg_index_t* peaks, int peak_count,
                             hls::stream<ecg_stream_word>& out_stream,
                             ecg_status& status) {
#pragma HLS INLINE off
    static ecg_coeff_t coeff_buf[ECG_MAX_LOSSY_SEGMENT];
    static ecg_coeff_t coeff_scratch[ECG_MAX_LOSSY_SEGMENT];
#pragma HLS BIND_STORAGE variable = coeff_buf type = ram_2p impl = bram
#pragma HLS BIND_STORAGE variable = coeff_scratch type = ram_2p impl = bram

    ecg_bitpacker packer;
    ecg_bitpack_init(packer);
    ecg_coder_out out = ecg_coder_pack(packer, out_stream);

    uint32_t qrs_count = 0;
    uint32_t saturated_total = 0;
    ecg_index_t cursor = 0;
    ecg_index_t prev_end = -1;
    int peak_i = 0;

    // First valid QRS window [start,end], or none (next_start == total).
    ecg_index_t next_start = total;
    ecg_index_t next_end = total - 1;
    bool have_qrs = false;
    while (peak_i < peak_count) {
#pragma HLS LOOP_TRIPCOUNT min = 0 max = ECG_MAX_PEAKS
        const ecg_index_t r = peaks[peak_i];
        ecg_index_t start = r - ECG_QRS_PRE;
        if (start < prev_end + 1) start = prev_end + 1;
        if (start < 0) start = 0;
        ecg_index_t end = r + ECG_QRS_POST;
        if (end > total - 1) end = total - 1;
        if (end < start) {
            peak_i++;
            continue;
        }
        next_start = start;
        next_end = end;
        have_qrs = true;
        break;
    }

encode_regions:
    while (cursor < total) {
#pragma HLS LOOP_TRIPCOUNT min = 1 max = (ECG_MAX_PEAKS * 2 + ECG_N_SAMPLES / ECG_MAX_LOSSY_SEGMENT)
        if (have_qrs && cursor == next_start) {
            ecg_encode_qrs(ecg_bram, next_start, (int)(next_end - next_start + 1), out);
            qrs_count++;
            cursor = next_end + 1;
            prev_end = next_end;
            peak_i++;
            have_qrs = false;
            next_start = total;
            while (peak_i < peak_count) {
#pragma HLS LOOP_TRIPCOUNT min = 0 max = 2
                const ecg_index_t r = peaks[peak_i];
                ecg_index_t start = r - ECG_QRS_PRE;
                if (start < prev_end + 1) start = prev_end + 1;
                if (start < 0) start = 0;
                ecg_index_t end = r + ECG_QRS_POST;
                if (end > total - 1) end = total - 1;
                if (end < start) {
                    peak_i++;
                    continue;
                }
                next_start = start;
                next_end = end;
                have_qrs = true;
                break;
            }
        } else {
            // One lossy call site only (gap before QRS or trailing) — do not inline
            // encode_nonqrs at two textual sites or HLS duplicates its pipelines.
            ecg_index_t limit = have_qrs ? next_start : total;
            ecg_index_t len = limit - cursor;
            if (len > ECG_MAX_LOSSY_SEGMENT) len = ECG_MAX_LOSSY_SEGMENT;
            int sat = 0;
            ecg_encode_nonqrs(ecg_bram, cursor, (int)len, coeff_buf, coeff_scratch, out,
                              &sat);
            saturated_total += (uint32_t)sat;
            cursor += len;
        }
    }

    ecg_bitpack_finish(packer, out_stream);

    status.word_count = packer.words_out;
    status.qrs_count = qrs_count;
    status.checksum = packer.checksum;
    status.peak_count = (uint32_t)peak_count;
    status.saturated = saturated_total;
}

void ecg_top(const ecg_bram_word_t* ecg_bram, uint32_t n_samples,
             hls::stream<ecg_stream_word>& out_stream, ecg_status& status) {
// depth is mandatory for C/RTL cosim: without it the wrapc adapter allocates a
// default-sized BRAM stub, so only a few samples are real and the rest read as
// garbage (few peaks, massive coefficient saturation, huge bitstream).
// Depth is in 32-bit words (two packed samples each), matching blk_mem_gen.
#pragma HLS INTERFACE bram port = ecg_bram depth = ECG_N_BRAM_WORDS storage_type = ram_1p latency = 2
#pragma HLS INTERFACE axis port = out_stream
#pragma HLS INTERFACE s_axilite port = n_samples bundle = ctrl
#pragma HLS INTERFACE s_axilite port = status bundle = ctrl
#pragma HLS INTERFACE s_axilite port = return bundle = ctrl

    // R-peak indices for the whole record. Sized for an unbroken 300 bpm rhythm,
    // which no physiological record approaches, so the array can never overflow.
    //
    // Block RAM (not LUTRAM): LUTRAM for 1502 x 32 burns ~750 LUTs. Detect then
    // encode are sequential, so a single-port BRAM is enough (~1–2 of 60 BRAM36).
    static ecg_index_t peaks[ECG_MAX_PEAKS];
#pragma HLS BIND_STORAGE variable = peaks type = ram_1p impl = bram

    const ecg_index_t total = (ecg_index_t)n_samples;

    // Pass 1: detection (exclusive sample-BRAM reader for this pass).
    const int peak_count = ecg_detect_pass(ecg_bram, total, peaks, ECG_MAX_PEAKS);

    // Pass 2: encode and pack inline (same process; AXIS can complete without a
    // concurrent DATAFLOW consumer).
    ecg_encode_stage(ecg_bram, total, peaks, peak_count, out_stream, status);
}
