#include "bitpack.h"

void ecg_bitpack_init(ecg_bitpacker& p) {
    p.accumulator = 0;
    p.count = 0;
    p.checksum = 0;
    p.words_out = 0;
    p.pending = 0;
    p.has_pending = false;
}

// Push one finished word through the single-word delay that lets TLAST be exact.
static void ecg_bitpack_emit(ecg_bitpacker& p, ecg_word_t word,
                             hls::stream<ecg_stream_word>& out) {
#pragma HLS INLINE
    if (p.has_pending) {
        ecg_stream_word previous;
        ecg_stream_set(previous, p.pending, false);
        out.write(previous);
    }
    p.pending = word;
    p.has_pending = true;
    p.checksum += word;          // wraps at 32 bits, matching the Python model
    p.words_out++;
}

void ecg_bitpack_put(ecg_bitpacker& p, uint32_t value, int nbits,
                     hls::stream<ecg_stream_word>& out) {
#pragma HLS INLINE off
    if (nbits <= 0) return;

    // Mask off anything above the declared width so a caller cannot corrupt
    // neighbouring fields by passing a wider value than it promised.
    const uint64_t masked =
        (nbits >= 32) ? (uint64_t)value
                      : ((uint64_t)value & (((uint64_t)1 << nbits) - 1));

    p.accumulator = (p.accumulator << nbits) | masked;
    p.count += nbits;

bitpack_emit:
    while (p.count >= ECG_WORD_BITS) {
#pragma HLS LOOP_TRIPCOUNT min = 0 max = 2
        p.count -= ECG_WORD_BITS;
        ecg_bitpack_emit(p, (ecg_word_t)(p.accumulator >> p.count), out);
    }
    // Drop the bits already emitted so the accumulator cannot overflow.
    if (p.count == 0) {
        p.accumulator = 0;
    } else {
        p.accumulator &= (((uint64_t)1 << p.count) - 1);
    }
}

int ecg_bitpack_align(ecg_bitpacker& p, hls::stream<ecg_stream_word>& out) {
#pragma HLS INLINE
    const int pad = (ECG_WORD_BITS - (p.count % ECG_WORD_BITS)) % ECG_WORD_BITS;
    if (pad > 0) ecg_bitpack_put(p, 0, pad, out);
    return pad;
}

void ecg_emit_bits(ecg_coder_out& o, uint32_t value, int nbits) {
#pragma HLS INLINE off
    if (o.pack_inline) {
        ecg_bitpack_put(*o.packer, value, nbits, *o.axis);
    } else {
        ecg_put_bits(*o.tokens, value, nbits);
    }
}

void ecg_emit_align(ecg_coder_out& o) {
    // Inline into callers and route pad bits through ecg_emit_bits so there is
    // only one bitpack_put instance (a separate INLINE-off align duplicated it).
#pragma HLS INLINE
    if (o.pack_inline) {
        const int pad =
            (ECG_WORD_BITS - (o.packer->count % ECG_WORD_BITS)) % ECG_WORD_BITS;
        if (pad > 0) ecg_emit_bits(o, 0, pad);
    } else {
        ecg_put_align(*o.tokens);
    }
}

void ecg_emit_op(ecg_coder_out& o, uint8_t op, uint32_t value) {
#pragma HLS INLINE off
    if (!o.pack_inline) {
        ecg_put_op(*o.tokens, op, value);
    }
}

void ecg_bitpack_finish(ecg_bitpacker& p, hls::stream<ecg_stream_word>& out) {
    if (!p.has_pending) return;
    ecg_stream_word final_word;
    ecg_stream_set(final_word, p.pending, true);   // the only TLAST in the frame
    out.write(final_word);
    p.has_pending = false;
}

void ecg_pack_stage(hls::stream<ecg_token>& tokens,
                    hls::stream<ecg_stream_word>& out, ecg_status& status) {
    ecg_bitpacker p;
    ecg_bitpack_init(p);

    uint32_t qrs_count = 0;
    uint32_t saturated = 0;
    uint32_t peak_count = 0;
    bool running = true;

pack_tokens:
    while (running) {
#pragma HLS LOOP_TRIPCOUNT min = 1 max = ECG_MAX_TOKENS
        const ecg_token t = tokens.read();
        switch (t.op) {
            case ECG_OP_BITS:
                ecg_bitpack_put(p, t.value, (int)t.nbits, out);
                break;
            case ECG_OP_ALIGN:
                ecg_bitpack_align(p, out);
                break;
            case ECG_OP_QRS:
                qrs_count = t.value;
                break;
            case ECG_OP_SAT:
                saturated = t.value;
                break;
            case ECG_OP_PEAKS:
                peak_count = t.value;
                break;
            default:                 // ECG_OP_END
                running = false;
                break;
        }
    }

    ecg_bitpack_finish(p, out);

    status.word_count = p.words_out;
    status.qrs_count = qrs_count;
    status.checksum = p.checksum;
    status.peak_count = peak_count;
    status.saturated = saturated;
}

void ecg_packer_module_top(hls::stream<ecg_token>& tokens,
                           hls::stream<ecg_stream_word>& out, ecg_status& status) {
#pragma HLS INTERFACE axis port = out
#pragma HLS INTERFACE ap_fifo port = tokens
#pragma HLS INTERFACE s_axilite port = status bundle = ctrl
#pragma HLS INTERFACE s_axilite port = return bundle = ctrl
    ecg_pack_stage(tokens, out, status);
}
