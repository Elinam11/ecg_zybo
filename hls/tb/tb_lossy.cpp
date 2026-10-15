// Module 3 testbench: bior4.4 + DZQ + cA DPCM/Rice + detail RLE/Rice.
#include <vector>

#include "lossy.h"
#include "tb_util.h"

int main(int argc, char** argv) {
    const char* record = (argc > 1) ? argv[1] : "100";
    const std::string dir = tb::golden_dir(record);
    std::cout << "tb_lossy: record " << record << " (bior4.4)" << std::endl;

    const std::vector<ecg_sample_t> samples =
        tb::load<ecg_sample_t>(dir + "/input_samples.txt");
    const std::vector<ecg_bram_word_t> bram = tb::pack_bram(samples);
    const std::vector<tb::Row> segment_rows = tb::load_rows(dir + "/segments.txt");
    // nonqrs_bw_words.txt: start end left_start left_len right_start right_len nwords w0 w1 ...
    const std::vector<tb::Row> gold = tb::load_rows(dir + "/nonqrs_bw_words.txt");

    tb::Checker check("tb_lossy");

    static ecg_coeff_t buf[ECG_MAX_LOSSY_SEGMENT];
    static ecg_coeff_t scratch[ECG_MAX_LOSSY_SEGMENT];

    size_t seg_index = 0;

    for (size_t s = 0; s < segment_rows.size(); s++) {
        if (segment_rows[s].fields.size() != 3 || segment_rows[s].fields[0] != 0) {
            continue;
        }
        if (seg_index >= gold.size()) {
            std::cerr << "  more non-QRS segments than golden rows\n";
            return 1;
        }
        const std::vector<long long>& expected = gold[seg_index].fields;
        const ecg_index_t start = (ecg_index_t)expected[0];
        const int length = (int)(expected[1] - expected[0] + 1);
        const ecg_index_t left_start = (ecg_index_t)expected[2];
        const int left_len = (int)expected[3];
        const ecg_index_t right_start = (ecg_index_t)expected[4];
        const int right_len = (int)expected[5];
        const int nwords = (int)expected[6];

        hls::stream<ecg_token> stream("tokens");
        ecg_coder_out out = ecg_coder_tokens(stream);
        int saturated = 0;
        ecg_encode_nonqrs(&bram[0], start, length, buf, scratch, out, &saturated,
                          left_start, left_len, right_start, right_len);

        const tb::Packed packed = tb::run_packer(stream);
        check.equal("nwords", (long long)packed.words.size(), (long long)nwords,
                    (long long)seg_index);
        const int cmp = (nwords < (int)packed.words.size()) ? nwords
                                                            : (int)packed.words.size();
        for (int w = 0; w < cmp; w++) {
            const long long want = expected[7 + w];
            check.equal("word", packed.words[w], want,
                        (long long)(seg_index * 100000 + w));
        }
        tb::check_tlast(check, packed.last);
        seg_index++;
    }

    check.equal("segment_count", (long long)seg_index, (long long)gold.size());
    return check.finish();
}
