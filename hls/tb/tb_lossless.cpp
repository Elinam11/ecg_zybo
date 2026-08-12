// Module 2 testbench: Golomb-Rice QRS coding, bit-exact against golden.
//
// Verifies the chosen k and the emitted packet length per window, then decodes the
// packet back in the testbench to prove exact reconstruction (PRD = 0% on QRS).
#include <vector>

#include "lossless.h"
#include "tb_util.h"

namespace {

// Bit reader over packed 32-bit words, mirroring python/ecg_ref/packer.py::BitReader.
class BitReader {
public:
    explicit BitReader(const std::vector<ecg_word_t>& words)
        : words_(words), pos_(0) {}

    uint32_t get(int nbits) {
        uint32_t value = 0;
        for (int i = 0; i < nbits; i++) {
            const size_t word = pos_ >> 5;
            const int bit = 31 - (int)(pos_ & 31);
            if (word >= words_.size()) {
                std::cerr << "FATAL: bitstream exhausted\n";
                std::exit(2);
            }
            value = (value << 1) | ((words_[word] >> bit) & 1u);
            pos_++;
        }
        return value;
    }

    void align32() { pos_ += (32 - (pos_ % 32)) % 32; }
    size_t tell() const { return pos_; }

private:
    const std::vector<ecg_word_t>& words_;
    size_t pos_;
};

int32_t sign_extend16(uint32_t value) {
    return (value & 0x8000u) ? (int32_t)(value | 0xFFFF0000u) : (int32_t)value;
}

// Decode one QRS packet. Independent of the encoder, so it is a genuine check rather
// than a restatement of the same logic.
std::vector<int32_t> decode_qrs(BitReader& reader, int length) {
    const int32_t anchor0 = sign_extend16(reader.get(16));
    const int32_t anchor1 = sign_extend16(reader.get(16));
    const int k = (int)reader.get(ECG_GR_K_BITS);

    std::vector<int32_t> out;
    out.push_back(anchor0);
    if (length > 1) out.push_back(anchor1);

    for (int i = ECG_ANCHOR_COUNT; i < length; i++) {
        int quotient = 0;
        bool escaped = false;
        while (true) {
            if (quotient == ECG_GR_Q_ESCAPE) {
                escaped = true;
                break;
            }
            if (reader.get(1) == 0) break;
            quotient++;
        }
        uint32_t mapped;
        if (escaped) {
            mapped = reader.get(ECG_GR_ESCAPE_BITS);
        } else {
            const uint32_t remainder = (k > 0) ? reader.get(k) : 0u;
            mapped = ((uint32_t)quotient << k) | remainder;
        }
        const int32_t error = ecg_unzigzag(mapped);
        const size_t m = out.size();
        out.push_back(ecg_predict(out[m - 1], out[m - 2]) + error);
    }
    return out;
}

}  // namespace

int main(int argc, char** argv) {
    const char* record = (argc > 1) ? argv[1] : "100";
    const std::string dir = tb::golden_dir(record);
    std::cout << "tb_lossless: record " << record << std::endl;

    const std::vector<ecg_sample_t> samples =
        tb::load<ecg_sample_t>(dir + "/input_samples.txt");
    const std::vector<ecg_bram_word_t> bram = tb::pack_bram(samples);
    const std::vector<tb::Row> segment_rows = tb::load_rows(dir + "/segments.txt");
    // qrs_params.txt: length anchor0 anchor1 k residual_sum escapes bit_count
    const std::vector<tb::Row> gold_params = tb::load_rows(dir + "/qrs_params.txt");

    tb::Checker check("tb_lossless");

    size_t window_index = 0;
    long long total_escapes = 0;
    for (size_t s = 0; s < segment_rows.size(); s++) {
        if (segment_rows[s].fields.size() != 3 || segment_rows[s].fields[0] != 1) {
            continue;
        }
        const ecg_index_t start = (ecg_index_t)segment_rows[s].fields[1];
        const ecg_index_t end = (ecg_index_t)segment_rows[s].fields[2];
        const int length = (int)(end - start + 1);

        if (window_index >= gold_params.size()) {
            std::cerr << "  more QRS windows than golden parameter rows\n";
            return 1;
        }
        const std::vector<long long>& expected = gold_params[window_index].fields;

        hls::stream<ecg_token> tokens("tokens");
        ecg_coder_out out = ecg_coder_tokens(tokens);
        const int k = ecg_encode_qrs(&bram[0], start, length, out);

        check.equal("length", length, expected[0], (long long)window_index);
        check.equal("anchor0", (int32_t)samples[start], expected[1],
                    (long long)window_index);
        if (length > 1) {
            check.equal("anchor1", (int32_t)samples[start + 1], expected[2],
                        (long long)window_index);
        }
        check.equal("k", k, expected[3], (long long)window_index);

        // Packet bits = tag + length field + payload, padded to a 32-bit boundary.
        const long long payload_bits = expected[6];
        const long long expect_bits = 1 + ECG_LEN_FIELD_BITS + payload_bits;
        const long long expect_words = (expect_bits + 31) / 32;
        const tb::Packed packed = tb::run_packer(tokens);
        const std::vector<ecg_word_t>& words = packed.words;
        tb::check_tlast(check, packed.last);
        check.equal("packet_words", (long long)words.size(), expect_words,
                    (long long)window_index);

        // ---- Decode and require exact reconstruction ----
        BitReader reader(words);
        const uint32_t tag = reader.get(1);
        const uint32_t coded_length = reader.get(ECG_LEN_FIELD_BITS);
        check.equal("tag", tag, ECG_TAG_QRS, (long long)window_index);
        check.equal("coded_length", coded_length, length, (long long)window_index);

        const std::vector<int32_t> decoded = decode_qrs(reader, (int)coded_length);
        check.equal("decoded_size", (long long)decoded.size(), length,
                    (long long)window_index);
        for (int i = 0; i < length && i < (int)decoded.size(); i++) {
            // This is the PRD = 0% requirement, asserted sample by sample.
            check.equal("lossless_sample", decoded[i], (int32_t)samples[start + i],
                        (long long)(start + i));
        }

        total_escapes += expected[5];
        window_index++;
    }

    check.equal("window_count", (long long)window_index, (long long)gold_params.size());

    std::ostringstream note;
    note << "windows=" << window_index << " golden escapes=" << total_escapes;
    check.note(note.str());
    return check.finish();
}
