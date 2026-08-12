// Top-level testbench: the complete frame must be bit-identical to the golden
// bitstream produced by the Python reference model.
//
// This is the strongest available check. It subsumes every module test: any divergence
// anywhere in detection, routing, either coder, or the packer changes the word stream.
#include <vector>

#include "ecg_top.h"
#include "tb_util.h"

int main(int argc, char** argv) {
    const char* record = (argc > 1) ? argv[1] : "100";
    const std::string dir = tb::golden_dir(record);
    std::cout << "tb_top: record " << record << std::endl;

    const std::vector<ecg_sample_t> samples =
        tb::load<ecg_sample_t>(dir + "/input_samples.txt");
    const std::vector<ecg_bram_word_t> bram = tb::pack_bram(samples);
    const std::vector<ecg_word_t> gold_words =
        tb::load<ecg_word_t>(dir + "/bitstream_words.txt");

    tb::Checker check("tb_top");

    hls::stream<ecg_stream_word> out("out_stream");
    ecg_status status;
    ecg_top(&bram[0], (uint32_t)samples.size(), out, status);

    // The core streams the payload; the PS prepends the header built from the status
    // registers. Reproduce that here so the comparison covers the whole frame.
    std::vector<ecg_word_t> frame(ECG_HEADER_WORDS, 0);
    ecg_build_header(status, (uint32_t)samples.size(), &frame[0]);
    std::vector<bool> last_flags;
    while (!out.empty()) {
        const ecg_stream_word word = out.read();
        frame.push_back(word.data);
        last_flags.push_back(word.last);
    }
    tb::check_tlast(check, last_flags);

    check.equal("frame_words", (long long)frame.size(), (long long)gold_words.size());
    check.equal_vec("bitstream", frame, gold_words);

    // Independent recomputation of the checksum over the payload words only.
    uint32_t checksum = 0;
    for (size_t i = ECG_HEADER_WORDS; i < frame.size(); i++) checksum += frame[i];
    check.equal("checksum_matches_payload", status.checksum, checksum);
    check.equal("payload_word_count", status.word_count,
                (uint32_t)(frame.size() - ECG_HEADER_WORDS));

    const double bits = (double)frame.size() * 32.0;
    const double cr = (double)samples.size() * ECG_SAMPLE_BITS / bits;
    std::ostringstream note;
    note << "words=" << frame.size() << " beats=" << status.qrs_count
         << " peaks=" << status.peak_count << " saturated=" << status.saturated
         << " CR=" << cr;
    check.note(note.str());

    return check.finish();
}
