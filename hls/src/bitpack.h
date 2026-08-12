#ifndef ECG_BITPACK_H
#define ECG_BITPACK_H

#include "ecg_types.h"

// MSB-first bit packer that emits whole 32-bit words onto an AXI4-Stream.
//
// Packing MSB-first and flushing on a 32-bit boundary is what lets the PS forward the
// payload to UART without ever realigning it, and lets the Python decoder read the
// stream back with the same bit order.
//
// TLAST handling: the core cannot know which word is final until the last segment has
// been coded, and buffering the whole payload to find out is impossible on this device.
// The packer therefore holds one word back, emitting word N-1 when word N arrives, so
// ecg_bitpack_finish() can mark the genuinely final word with last = true. The cost is
// a single register of latency.
struct ecg_bitpacker {
    uint64_t accumulator;   // pending bits, right-aligned within the low `count` bits
    int count;              // number of valid bits held in the accumulator
    ecg_word_t checksum;    // running 32-bit additive checksum of produced words
    uint32_t words_out;     // words produced (including the one still held back)
    ecg_word_t pending;     // word held back so TLAST can be placed correctly
    bool has_pending;
};

void ecg_bitpack_init(ecg_bitpacker& p);

// Append `nbits` (0..32) of `value`, emitting complete words to `out`.
void ecg_bitpack_put(ecg_bitpacker& p, uint32_t value, int nbits,
                     hls::stream<ecg_stream_word>& out);

// Zero-pad to the next 32-bit boundary. Returns the number of pad bits inserted.
int ecg_bitpack_align(ecg_bitpacker& p, hls::stream<ecg_stream_word>& out);

// Flush the held-back word with last = true. Must be called exactly once, after the
// final segment, or the receiver will never see end-of-frame.
void ecg_bitpack_finish(ecg_bitpacker& p, hls::stream<ecg_stream_word>& out);

// ---------------------------------------------------------------------------
// Coder output sink: either a token stream (module TBs / packer stage) or the
// bitpacker+AXIS path used by ecg_top.
//
// ecg_top packs inline in one process so the XC7Z010 BRAM budget is not spent on a
// deep token FIFO, and so RTL cosim is not exposed to the DATAFLOW+AXIS deadlock
// (packer blocked on TREADY while the TB waits for ap_done).
// ---------------------------------------------------------------------------
struct ecg_coder_out {
    bool pack_inline;
    hls::stream<ecg_token>* tokens;
    ecg_bitpacker* packer;
    hls::stream<ecg_stream_word>* axis;
};

static inline ecg_coder_out ecg_coder_tokens(hls::stream<ecg_token>& tokens) {
#pragma HLS INLINE
    ecg_coder_out o;
    o.pack_inline = false;
    o.tokens = &tokens;
    o.packer = 0;
    o.axis = 0;
    return o;
}

static inline ecg_coder_out ecg_coder_pack(ecg_bitpacker& packer,
                                          hls::stream<ecg_stream_word>& out) {
#pragma HLS INLINE
    ecg_coder_out o;
    o.pack_inline = true;
    o.tokens = 0;
    o.packer = &packer;
    o.axis = &out;
    return o;
}

// Not header-inline: keeps a real call to ecg_bitpack_put. QRS/non-QRS are inlined
// into ecg_encode_stage so both paths share one emit/bitpack body (do not use
// ALLOCATION for this — Vitis HLS 2023.2 crashes with HLS 200-1715 on this design).
void ecg_emit_bits(ecg_coder_out& o, uint32_t value, int nbits);
void ecg_emit_align(ecg_coder_out& o);
void ecg_emit_op(ecg_coder_out& o, uint8_t op, uint32_t value);

// DATAFLOW sink process: drain the coder's token FIFO, pack the bit fields into 32-bit
// words, drive the AXI4-Stream, and publish the run statistics.
//
// This is the only writer of `status`, which is what makes the AXI4-Lite status
// registers safe to expose from inside a DATAFLOW region.
//
// Runs until it reads ECG_OP_END, so the producer must always terminate the stream.
void ecg_pack_stage(hls::stream<ecg_token>& tokens,
                    hls::stream<ecg_stream_word>& out, ecg_status& status);

// Standalone synthesis top for the packer module only.
//
// ecg_bitpack_put cannot be set_top: its `out` argument is hls::stream<ap_axiu<...>>
// without an INTERFACE axis pragma, and HLS 214-208 rejects ap_axiu on non-axis
// top ports. This wrapper is the module-level top; ecg_top remains the system top.
void ecg_packer_module_top(hls::stream<ecg_token>& tokens,
                           hls::stream<ecg_stream_word>& out, ecg_status& status);

#endif  // ECG_BITPACK_H
