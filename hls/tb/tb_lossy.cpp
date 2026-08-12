// Module 3 testbench: Haar DWT + thresholding + RLE, bit-exact against golden.
//
// The golden file records, per non-QRS segment, the packed 16-bit RLE tokens the
// reference model produced. Any divergence in the transform, the threshold rule, the
// rounding, or the run-length overflow handling shows up as a token mismatch.
#include <vector>

#include "lossy.h"
#include "tb_util.h"

int main(int argc, char** argv) {
    const char* record = (argc > 1) ? argv[1] : "100";
    const std::string dir = tb::golden_dir(record);
    std::cout << "tb_lossy: record " << record << std::endl;

    const std::vector<ecg_sample_t> samples =
        tb::load<ecg_sample_t>(dir + "/input_samples.txt");
    const std::vector<ecg_bram_word_t> bram = tb::pack_bram(samples);
    const std::vector<tb::Row> segment_rows = tb::load_rows(dir + "/segments.txt");
    // nonqrs_tokens.txt: length padded_length token_count token...
    const std::vector<tb::Row> gold = tb::load_rows(dir + "/nonqrs_tokens.txt");

    tb::Checker check("tb_lossy");

    static ecg_coeff_t buf[ECG_MAX_LOSSY_SEGMENT];
    static ecg_coeff_t scratch[ECG_MAX_LOSSY_SEGMENT];

    size_t seg_index = 0;
    long long total_tokens = 0;
    long long total_saturated = 0;

    for (size_t s = 0; s < segment_rows.size(); s++) {
        if (segment_rows[s].fields.size() != 3 || segment_rows[s].fields[0] != 0) {
            continue;
        }
        const ecg_index_t start = (ecg_index_t)segment_rows[s].fields[1];
        const ecg_index_t end = (ecg_index_t)segment_rows[s].fields[2];
        const int length = (int)(end - start + 1);

        if (seg_index >= gold.size()) {
            std::cerr << "  more non-QRS segments than golden rows\n";
            return 1;
        }
        const std::vector<long long>& expected = gold[seg_index].fields;

        hls::stream<ecg_token> stream("tokens");
        ecg_coder_out out = ecg_coder_tokens(stream);
        int saturated = 0;
        const int tokens = ecg_encode_nonqrs(&bram[0], start, length, buf, scratch, out,
                                             &saturated);

        check.equal("length", length, expected[0], (long long)seg_index);
        check.equal("padded_length", ecg_dwt_padded_len(length), expected[1],
                    (long long)seg_index);
        check.equal("token_count", tokens, expected[2], (long long)seg_index);

        // Re-read the packet and compare every token against the golden stream.
        const tb::Packed packed = tb::run_packer(stream);
        const std::vector<ecg_word_t>& words = packed.words;
        tb::check_tlast(check, packed.last);

        size_t pos = 0;
        const size_t total_bits = words.size() * 32;
        auto get_bits = [&](int nbits) -> uint32_t {
            uint32_t value = 0;
            for (int i = 0; i < nbits; i++) {
                if (pos >= total_bits) {
                    std::cerr << "FATAL: packet exhausted\n";
                    std::exit(2);
                }
                const int bit = 31 - (int)(pos & 31);
                value = (value << 1) | ((words[pos >> 5] >> bit) & 1u);
                pos++;
            }
            return value;
        };

        check.equal("tag", get_bits(1), ECG_TAG_NONQRS, (long long)seg_index);
        check.equal("coded_length", get_bits(ECG_LEN_FIELD_BITS), length,
                    (long long)seg_index);
        const uint32_t coded_tokens = get_bits(ECG_SYMCOUNT_BITS);
        check.equal("coded_token_count", coded_tokens, expected[2],
                    (long long)seg_index);

        for (uint32_t t = 0; t < coded_tokens; t++) {
            const uint32_t token = get_bits(ECG_RLE_TOKEN_BITS);
            const long long want = (t + 3 < expected.size()) ? expected[t + 3] : -1;
            check.equal("token", token, want, (long long)(seg_index * 100000 + t));

            // Structural: the run field must never exceed its 6-bit range, and a zero
            // value field must only ever appear as a full-length continuation marker.
            const uint32_t run = (token >> ECG_RLE_VAL_BITS) & ECG_RLE_RUN_MAX;
            const uint32_t value = token & ((1u << ECG_RLE_VAL_BITS) - 1u);
            check.equal("run_in_range", run <= (uint32_t)ECG_RLE_RUN_MAX, 1);
            if (value == 0) {
                check.equal("continuation_run_is_max", run,
                            (uint32_t)ECG_RLE_RUN_MAX);
            }
        }

        // The packet must end on a 32-bit boundary.
        check.equal("word_aligned", (long long)((pos + 31) / 32),
                    (long long)words.size(), (long long)seg_index);

        total_tokens += tokens;
        total_saturated += saturated;
        seg_index++;
    }

    check.equal("segment_count", (long long)seg_index, (long long)gold.size());

    std::ostringstream note;
    note << "segments=" << seg_index << " tokens=" << total_tokens
         << " saturated=" << total_saturated;
    check.note(note.str());

    // ---- Reversibility of the transform, independent of the golden vectors ----
    {
        for (int len = 16; len <= 512; len *= 2) {
            for (int i = 0; i < len; i++) {
                buf[i] = (ecg_coeff_t)((i * 7919) % 1001 - 500);
            }
            std::vector<ecg_coeff_t> original(buf, buf + len);
            ecg_haar_forward(buf, len, scratch);

            // Inverse, mirroring python/ecg_ref/lossy.py::_inverse_haar_step.
            int level_len = len >> (ECG_DWT_LEVELS - 1);
            for (int level = ECG_DWT_LEVELS; level >= 1; level--) {
                const int half = level_len >> 1;
                for (int m = 0; m < half; m++) {
                    const ecg_coeff_t a = buf[m];
                    const ecg_coeff_t d = buf[half + m];
                    const ecg_coeff_t odd = a - (ecg_coeff_t)ecg_asr(d, 1);
                    scratch[2 * m] = d + odd;
                    scratch[2 * m + 1] = odd;
                }
                for (int i = 0; i < level_len; i++) buf[i] = scratch[i];
                level_len <<= 1;
            }
            for (int i = 0; i < len; i++) {
                check.equal("haar_reversible", buf[i], original[i], i);
            }
        }
    }

    return check.finish();
}
