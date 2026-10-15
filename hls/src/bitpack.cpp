#include "bitpack.h"

// Set by ecg_bitpack_init. The packer registers live in ecg_pack_op and are cleared
// on the next call, so init does not need the stream.
static bool g_pack_reset = true;

uint32_t ecg_hold_u32(uint32_t x);

void ecg_bitpack_init(ecg_bitpacker& p) {
    p.accumulator = 0;
    p.count = 0;
    p.checksum = 0;
    p.words_out = 0;
    p.pending = 0;
    p.has_pending = false;
    g_pack_reset = true;
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

// (1<<n)-1 for n = 0..31. A variable shift here was a 4.4 ns barrel shifter on the
// packer's critical path; the table is a 32-word ROM.
static uint32_t ecg_low_mask(int n) {
#pragma HLS INLINE
    static const uint32_t m[32] = {
        0x00000000u, 0x00000001u, 0x00000003u, 0x00000007u, 0x0000000fu, 0x0000001fu,
        0x0000003fu, 0x0000007fu, 0x000000ffu, 0x000001ffu, 0x000003ffu, 0x000007ffu,
        0x00000fffu, 0x00001fffu, 0x00003fffu, 0x00007fffu, 0x0000ffffu, 0x0001ffffu,
        0x0003ffffu, 0x0007ffffu, 0x000fffffu, 0x001fffffu, 0x003fffffu, 0x007fffffu,
        0x00ffffffu, 0x01ffffffu, 0x03ffffffu, 0x07ffffffu, 0x0fffffffu, 0x1fffffffu,
        0x3fffffffu, 0x7fffffffu};
    if (n <= 0) return 0u;
    if (n >= 32) return 0xffffffffu;
    return m[n];
}

// One shifter. Left and right run in the same loop so HLS does not clone a second
// copy (two outline calls were 246 LUT each).
static void ecg_shift_pair(uint32_t& acc, int sh_left, uint32_t& val, int sh_right) {
#pragma HLS INLINE off
    int n = sh_left;
    if (sh_right > n) n = sh_right;
    if (n < 0) n = 0;
    if (n > 32) n = 32;
    for (int i = 0; i < n; i++) {
#pragma HLS PIPELINE off
#pragma HLS UNROLL off
#pragma HLS LOOP_TRIPCOUNT min = 0 max = 32
        if (i < sh_left) acc <<= 1;
        if (i < sh_right) val >>= 1;
    }
}

// Packer registers stay in this function. Passing them through every Rice call made
// encode_stage carry an 8-word phi (about 4400 LUT of muxes) and a combinational
// return that the loop predicate was added to (slack about -1.6 ns).
enum { PK_PUT = 1, PK_COUNT = 5, PK_FINISH = 6, PK_WORDS = 7, PK_CKSUM = 8 };

static uint32_t ecg_pack_op(int op, uint32_t a, int b,
                            hls::stream<ecg_stream_word>& out) {
#pragma HLS INLINE off
    static ecg_bitpacker st;
    if (g_pack_reset) {
        st.accumulator = 0;
        st.count = 0;
        st.checksum = 0;
        st.words_out = 0;
        st.pending = 0;
        st.has_pending = false;
        g_pack_reset = false;
    }

    if (op == PK_COUNT) {
        // Sequencing token from ecg_hold_u32. a^a is zero; the XOR keeps the
        // call from being scheduled in the caller's previous cycle.
        const uint32_t z = a ^ a;
        return (uint32_t)st.count + z;
    }
    if (op == PK_WORDS) return st.words_out;
    if (op == PK_CKSUM) return st.checksum;
    if (op == PK_FINISH) {
        // `a` is the sequencing token. a^a is zero and does not change TLAST.
        const uint32_t z = a ^ a;
        if (st.has_pending) {
            ecg_stream_word final_word;
            ecg_stream_set(final_word, st.pending + z, true);
            out.write(final_word);
            st.has_pending = false;
        }
        return 0;
    }

    // PK_PUT. Three phases so FSM decode + (count+nbits) + (total-32) are not
    // one 24-LUT combinational path (that closed impl at WNS -2.04 ns).
    uint32_t value = a;
    int nbits = b;
    int total_r = 0;
    for (int phase = 0; phase < 3; phase++) {
#pragma HLS PIPELINE off
#pragma HLS UNROLL off
#pragma HLS LOOP_FLATTEN off
        if (phase == 0) {
            value = a;
            nbits = b;
        } else if (phase == 1) {
            total_r = st.count + nbits;
        } else if (nbits > 0) {
                const uint32_t masked =
                    (nbits >= 32) ? value : (value & ecg_low_mask(nbits));
                const int total = total_r;
                const bool carry = total >= ECG_WORD_BITS;
                const int rem = carry ? (total - ECG_WORD_BITS) : 0;
                const int fill = carry ? (nbits - rem) : nbits;
                uint32_t part_acc = st.accumulator;
                uint32_t part_val = masked;
                ecg_shift_pair(part_acc, fill, part_val, carry ? rem : 0);
                if (!carry) {
                    st.accumulator = part_acc | masked;
                    st.count = total;
                } else {
                    const ecg_word_t word = (fill >= ECG_WORD_BITS)
                        ? (ecg_word_t)masked
                        : (ecg_word_t)(part_acc | part_val);
                    ecg_bitpack_emit(st, word, out);
                    st.count = rem;
                    st.accumulator = (rem == 0) ? 0u : (masked & ecg_low_mask(rem));
                }
        }
    }
    return 0;
}

void ecg_bitpack_put(ecg_bitpacker& p, uint32_t value, int nbits,
                     hls::stream<ecg_stream_word>& out) {
#pragma HLS INLINE
    (void)p;
    ecg_pack_op(PK_PUT, value, nbits, out);
}

int ecg_bitpack_align(ecg_bitpacker& p, hls::stream<ecg_stream_word>& out) {
#pragma HLS INLINE
    (void)p;
    const int count = (int)ecg_pack_op(PK_COUNT, 0, 0, out);
    const int pad = (ECG_WORD_BITS - (count % ECG_WORD_BITS)) % ECG_WORD_BITS;
    if (pad > 0) ecg_bitpack_put(p, 0, pad, out);
    return pad;
}

// Register the value for one loop trip before it is returned. A plain `return x`
// with LATENCY was deleted, so the caller's predicate stayed in series with the
// packer (encode_stage slack -1.84 ns).
uint32_t ecg_hold_u32(uint32_t x) {
#pragma HLS INLINE off
    static uint32_t r;
    int n = 2;
    for (int i = 0; i < n; i++) {
#pragma HLS PIPELINE off
#pragma HLS UNROLL off
#pragma HLS LOOP_TRIPCOUNT min = 2 max = 2
        if (i == 0) r = x;
    }
    return r;
}

static void ecg_emit_bits_raw(ecg_coder_out& o, uint32_t value, int nbits) {
    // Inline so every packer call sits in ecg_encode_stage. A separate module
    // here instantiated a second ecg_pack_op with its own registers.
#pragma HLS INLINE
    if (o.pack_inline) {
        ecg_bitpack_put(*o.packer, value, nbits, *o.axis);
    } else {
        ecg_put_bits(*o.tokens, value, nbits);
    }
}

void ecg_emit_bits(ecg_coder_out& o, uint32_t value, int nbits) {
#pragma HLS INLINE
    // Both arguments are registered. A bare nbits add in the same cycle as the
    // packer call was 2.55 + 6.45 = 9.00 ns.
    const uint32_t v = ecg_hold_u32(value);
    const uint32_t n = ecg_hold_u32((uint32_t)nbits);
    ecg_emit_bits_raw(o, v, (int)n);
}

static uint32_t ecg_rice_unary_word(uint32_t q) {
#pragma HLS INLINE off
    // q ones followed by a zero. q is 0..30 here; the escape path never calls this.
    return ecg_low_mask((int)q) << 1;
}

// k is 0..15. A variable barrel here was 4.1 ns and sat in the same cycle as the
// packer handshake (encode_stage slack -2.07 ns).
static uint32_t ecg_shr_k(uint32_t value, int k) {
#pragma HLS INLINE off
    for (int i = 0; i < k; i++) {
#pragma HLS PIPELINE off
#pragma HLS UNROLL off
#pragma HLS LOOP_TRIPCOUNT min = 0 max = 15
        value >>= 1;
    }
    return value;
}

void ecg_emit_golomb_rice(ecg_coder_out& o, uint32_t value, int k) {
#pragma HLS INLINE
    if (k < 0) k = 0;
    if (k > ECG_GR_K_MAX) k = ECG_GR_K_MAX;
    const uint32_t q = (k > 0) ? ecg_shr_k(value, k) : value;
    const uint32_t r = (k > 0) ? (value & ecg_low_mask(k)) : 0;
    if (q >= (uint32_t)ECG_GR_Q_ESCAPE) {
        ecg_emit_bits(o, 0x7FFFFFFFu, ECG_GR_Q_ESCAPE);
        ecg_emit_bits(o, value, ECG_GR_ESCAPE_BITS);
    } else {
        ecg_emit_bits(o, ecg_rice_unary_word(q), (int)q + 1);
        if (k > 0) ecg_emit_bits(o, r, k);
    }
}

void ecg_emit_align(ecg_coder_out& o) {
    // Inline into callers and route pad bits through ecg_emit_bits so there is
    // only one bitpack_put instance (a separate INLINE-off align duplicated it).
#pragma HLS INLINE
    if (o.pack_inline) {
        // `gate` is a sequencing token so the count read is not in the same cycle
        // as the caller's loop-exit compare.
        const uint32_t gate = ecg_hold_u32(0);
        const int count = (int)ecg_pack_op(PK_COUNT, gate, 0, *o.axis);
        const int pad = (ECG_WORD_BITS - (count % ECG_WORD_BITS)) % ECG_WORD_BITS;
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
    // Data edge from the hold, not a compare: a compare on the hold result was
    // landing in the same cycle as the packer call.
    const uint32_t gate = ecg_hold_u32(1);
    ecg_pack_op(PK_FINISH, gate, 0, out);
    p.words_out = ecg_pack_op(PK_WORDS, 0, 0, out);
    p.checksum = ecg_pack_op(PK_CKSUM, 0, 0, out);
    p.count = (int)ecg_pack_op(PK_COUNT, 0, 0, out);
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
