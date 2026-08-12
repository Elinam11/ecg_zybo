// Module 1 testbench: every stage of the detection chain, bit-exact against golden.
#include <vector>

#include "detect.h"
#include "tb_util.h"

int main(int argc, char** argv) {
    const char* record = (argc > 1) ? argv[1] : "100";
    const std::string dir = tb::golden_dir(record);
    std::cout << "tb_detect: record " << record << " (" << dir << ")" << std::endl;

    const std::vector<ecg_sample_t> samples =
        tb::load<ecg_sample_t>(dir + "/input_samples.txt");
    const std::vector<ecg_bram_word_t> bram = tb::pack_bram(samples);
    const std::vector<ecg_filt_t> gold_bp =
        tb::load<ecg_filt_t>(dir + "/bandpass.txt");
    const std::vector<ecg_energy_t> gold_tkeo =
        tb::load<ecg_energy_t>(dir + "/tkeo.txt");
    const std::vector<ecg_energy_t> gold_smooth =
        tb::load<ecg_energy_t>(dir + "/smooth.txt");
    const std::vector<ecg_energy_t> gold_thr =
        tb::load<ecg_energy_t>(dir + "/threshold.txt");
    const std::vector<ecg_index_t> gold_triggers =
        tb::load<ecg_index_t>(dir + "/triggers.txt");
    const std::vector<ecg_index_t> gold_peaks =
        tb::load<ecg_index_t>(dir + "/r_peaks.txt");

    const ecg_index_t n = (ecg_index_t)samples.size();
    tb::Checker check("tb_detect");

    // ---- Full chain with trace capture ----
    std::vector<ecg_filt_t> bp(n, 0);
    std::vector<ecg_energy_t> psi(n, 0), smooth(n, 0), thr(n, 0);
    std::vector<ecg_index_t> triggers(ECG_MAX_PEAKS, 0);

    const int trigger_count =
        ecg_detect_triggers(&bram[0], n, &triggers[0], ECG_MAX_PEAKS,
                            &bp[0], &psi[0], &smooth[0], &thr[0]);
    triggers.resize(trigger_count);

    check.equal_vec("bandpass", bp, gold_bp);
    check.equal_vec("tkeo", psi, gold_tkeo);
    check.equal_vec("smooth", smooth, gold_smooth);
    check.equal_vec("threshold", thr, gold_thr);
    check.equal_vec("triggers", triggers, gold_triggers);

    // ---- Apex refinement ----
    std::vector<ecg_index_t> peaks(triggers);
    for (size_t i = 0; i < peaks.size(); i++) {
        peaks[i] = ecg_search_peak(&bram[0], n, triggers[i]);
    }
    check.equal_vec("r_peaks", peaks, gold_peaks);

    // ---- Window framing against the golden segment table ----
    const std::vector<tb::Row> segment_rows = tb::load_rows(dir + "/segments.txt");
    std::vector<ecg_index_t> gold_starts, gold_ends;
    for (size_t i = 0; i < segment_rows.size(); i++) {
        if (segment_rows[i].fields.size() == 3 && segment_rows[i].fields[0] == 1) {
            gold_starts.push_back((ecg_index_t)segment_rows[i].fields[1]);
            gold_ends.push_back((ecg_index_t)segment_rows[i].fields[2]);
        }
    }

    std::vector<ecg_index_t> starts(ECG_MAX_PEAKS, 0), ends(ECG_MAX_PEAKS, 0);
    const int window_count = ecg_frame_windows(&peaks[0], (int)peaks.size(), n,
                                               &starts[0], &ends[0], ECG_MAX_PEAKS);
    starts.resize(window_count);
    ends.resize(window_count);
    check.equal_vec("window_start", starts, gold_starts);
    check.equal_vec("window_end", ends, gold_ends);

    // ---- Structural invariants the spec calls out for Module 1 ----
    for (int i = 0; i < window_count; i++) {
        const int length = ends[i] - starts[i] + 1;
        check.equal("window_positive", length > 0, 1, i);
        check.equal("window_within_nominal", length <= ECG_QRS_WINDOW, 1, i);
        if (i > 0) {
            // Rapid consecutive beats must truncate, never overlap.
            check.equal("window_no_overlap", starts[i] > ends[i - 1], 1, i);
        }
    }
    for (int i = 1; i < trigger_count; i++) {
        check.equal("refractory_respected",
                    triggers[i] - triggers[i - 1] > ECG_REFRACTORY, 1, i);
    }

    std::ostringstream note;
    note << "triggers=" << trigger_count << " peaks=" << peaks.size()
         << " windows=" << window_count;
    check.note(note.str());
    return check.finish();
}
