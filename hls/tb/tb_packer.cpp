// Module 4 testbench: bit packing order, word alignment and checksum.
//
// Self-contained (no golden vectors): these are the invariants the Python BitWriter and
// the hardware packer must share for any frame to be decodable at all.
#include <vector>

#include "bitpack.h"
#include "ecg_top.h"
#include "tb_util.h"

namespace {

// Flush the held-back word, then collect everything the packer produced.
std::vector<ecg_word_t> drain(ecg_bitpacker& p, hls::stream<ecg_stream_word>& s) {
    ecg_bitpack_finish(p, s);
    std::vector<ecg_word_t> out;
    while (!s.empty()) out.push_back(s.read().data);
    return out;
}

}  // namespace

int main() {
    std::cout << "tb_packer" << std::endl;
    tb::Checker check("tb_packer");

    // ---- MSB-first ordering ----
    {
        hls::stream<ecg_stream_word> s("s");
        ecg_bitpacker p;
        ecg_bitpack_init(p);
        ecg_bitpack_put(p, 0xBu, 4, s);     // 1011 must land in the top nibble
        ecg_bitpack_put(p, 0u, 28, s);
        const std::vector<ecg_word_t> words = drain(p, s);
        check.equal("msb_first_words", (long long)words.size(), 1);
        if (!words.empty()) check.equal("msb_first_value", words[0], 0xB0000000u);
    }

    // ---- A full 32-bit field emits exactly one word ----
    {
        hls::stream<ecg_stream_word> s("s");
        ecg_bitpacker p;
        ecg_bitpack_init(p);
        ecg_bitpack_put(p, 0xDEADBEEFu, 32, s);
        const std::vector<ecg_word_t> words = drain(p, s);
        check.equal("full_word_count", (long long)words.size(), 1);
        if (!words.empty()) check.equal("full_word_value", words[0], 0xDEADBEEFu);
        check.equal("no_residual_bits", p.count, 0);
    }

    // ---- Fields straddling a word boundary ----
    {
        hls::stream<ecg_stream_word> s("s");
        ecg_bitpacker p;
        ecg_bitpack_init(p);
        for (int i = 0; i < 4; i++) ecg_bitpack_put(p, 0xFFFFu, 16, s);
        const std::vector<ecg_word_t> words = drain(p, s);
        check.equal("straddle_word_count", (long long)words.size(), 2);
        for (size_t i = 0; i < words.size(); i++) {
            check.equal("straddle_value", words[i], 0xFFFFFFFFu, (long long)i);
        }
    }

    // ---- Alignment padding ----
    {
        hls::stream<ecg_stream_word> s("s");
        ecg_bitpacker p;
        ecg_bitpack_init(p);
        ecg_bitpack_put(p, 1u, 1, s);
        const int pad = ecg_bitpack_align(p, s);
        check.equal("align_pad_bits", pad, 31);
        const std::vector<ecg_word_t> words = drain(p, s);
        check.equal("align_word_count", (long long)words.size(), 1);
        if (!words.empty()) check.equal("align_value", words[0], 0x80000000u);
        check.equal("align_is_idempotent", ecg_bitpack_align(p, s), 0);
    }

    // ---- Over-wide values must not corrupt neighbouring fields ----
    {
        hls::stream<ecg_stream_word> s("s");
        ecg_bitpacker p;
        ecg_bitpack_init(p);
        ecg_bitpack_put(p, 0xFFFFFFFFu, 4, s);   // only the low 4 bits are promised
        ecg_bitpack_put(p, 0u, 28, s);
        const std::vector<ecg_word_t> words = drain(p, s);
        if (!words.empty()) check.equal("masked_value", words[0], 0xF0000000u);
    }

    // ---- Additive checksum matches a straight sum of emitted words ----
    {
        hls::stream<ecg_stream_word> s("s");
        ecg_bitpacker p;
        ecg_bitpack_init(p);
        for (uint32_t i = 1; i <= 64; i++) {
            ecg_bitpack_put(p, 0x01000000u * i + i, 32, s);
        }
        const std::vector<ecg_word_t> words = drain(p, s);
        uint32_t expected = 0;
        for (size_t i = 0; i < words.size(); i++) expected += words[i];
        check.equal("checksum", p.checksum, expected);
        check.equal("word_count", p.words_out, (uint32_t)words.size());
    }

    // ---- Token protocol: the coder -> packer channel used by DATAFLOW ----
    // Driving the packer through the FIFO must produce exactly the same words as
    // calling it directly, and must fold the meta tokens into the status registers.
    {
        hls::stream<ecg_token> tokens("tokens");
        ecg_put_bits(tokens, 0xAu, 4);
        ecg_put_align(tokens);                  // -> 0xA0000000
        ecg_put_bits(tokens, 0xDEADBEEFu, 32);
        ecg_put_op(tokens, ECG_OP_QRS, 368);
        ecg_put_op(tokens, ECG_OP_SAT, 7);
        ecg_put_op(tokens, ECG_OP_PEAKS, 369);

        const tb::Packed packed = tb::run_packer(tokens);
        check.equal("token_word_count", (long long)packed.words.size(), 2);
        if (packed.words.size() == 2) {
            check.equal("token_word0", packed.words[0], 0xA0000000u);
            check.equal("token_word1", packed.words[1], 0xDEADBEEFu);
        }
        tb::check_tlast(check, packed.last);
        check.equal("status_words", packed.status.word_count, 2u);
        check.equal("status_qrs", packed.status.qrs_count, 368u);
        check.equal("status_sat", packed.status.saturated, 7u);
        check.equal("status_peaks", packed.status.peak_count, 369u);
        check.equal("status_checksum", packed.status.checksum,
                    0xA0000000u + 0xDEADBEEFu);
    }

    // ---- Header assembly, shared with the PS firmware ----
    {
        ecg_status status;
        status.word_count = 1234;
        status.qrs_count = 368;
        status.checksum = 0x12345678u;
        status.peak_count = 368;
        status.saturated = 0;

        ecg_word_t header[ECG_HEADER_WORDS];
        ecg_build_header(status, ECG_N_SAMPLES, header);
        check.equal("magic", header[0], ECG_MAGIC_WORD);
        check.equal("sample_count", header[1], (uint32_t)ECG_N_SAMPLES);
        check.equal("fs_field", header[2] & 0xFFFFu, (uint32_t)ECG_FS);
        check.equal("qrs_field", (header[2] >> 16) & 0xFFFFu, 368u);
        check.equal("checksum_field", header[3], 0x12345678u);
    }

    return check.finish();
}
