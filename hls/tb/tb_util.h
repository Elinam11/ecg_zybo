// Shared testbench helpers: golden-vector loading and bit-exact comparison.
//
// Golden files are plain text, one integer per line, emitted by
// python/ecg_ref/pipeline.py::write_golden. Plain text keeps the testbenches free of
// any parsing dependency and makes a mismatch trivially diffable.
#ifndef TB_UTIL_H
#define TB_UTIL_H

#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "bitpack.h"

namespace tb {

// Run the packer stage over a completed token stream and collect everything it
// produced. Mirrors what ecg_top does, so a module testbench sees exactly the words
// the hardware would put on the AXI4-Stream.
struct Packed {
    std::vector<ecg_word_t> words;
    std::vector<bool> last;
    ecg_status status;
};

inline Packed run_packer(hls::stream<ecg_token>& tokens) {
    ecg_put_op(tokens, ECG_OP_END, 0);
    hls::stream<ecg_stream_word> out("out");
    Packed packed;
    ecg_pack_stage(tokens, out, packed.status);
    while (!out.empty()) {
        const ecg_stream_word word = out.read();
        packed.words.push_back(word.data);
        packed.last.push_back(word.last);
    }
    return packed;
}


inline std::string golden_dir(const char* record) {
    // Overridable so the Tcl flow can point at a different data root.
    const char* root = std::getenv("ECG_GOLDEN_DIR");
    std::string base = root ? root : "../../../data/golden";
    return base + "/" + record;
}

inline std::string coe_dir() {
    const char* root = std::getenv("ECG_COE_DIR");
    return root ? root : "../../../data/coe";
}

// Pack int16 samples into the 32-bit BRAM layout used by hardware / .coe files.
//
// Always pad to ECG_N_BRAM_WORDS. Cosim wrapc copies the full INTERFACE `depth`
// from the TB pointer (see apatb_*_ir.ll arraycpy of 54000 words). A short cosim
// excerpt that only allocates packed(samples) words overruns the heap and
// segfaults during C post-check (COSIM 212-379).
inline std::vector<ecg_bram_word_t> pack_bram(const std::vector<ecg_sample_t>& samples) {
    std::vector<ecg_bram_word_t> words(ECG_N_BRAM_WORDS, 0);
    const size_t n = samples.size();
    for (size_t i = 0; i < n; i++) {
        const uint32_t half = (uint16_t)samples[i];
        if (i & 1u) words[i >> 1] |= (half << 16);
        else words[i >> 1] |= half;
    }
    return words;
}

// Load whitespace-separated integers from a file. Fatal if the file is missing, since
// a silently empty vector would make every comparison vacuously pass.
template <typename T>
std::vector<T> load(const std::string& path) {
    std::ifstream in(path.c_str());
    if (!in) {
        std::cerr << "FATAL: cannot open golden file " << path << "\n"
                  << "       run: python python/scripts/run_reference.py <rec> --golden"
                  << std::endl;
        std::exit(2);
    }
    std::vector<T> out;
    long long value;
    while (in >> value) out.push_back((T)value);
    if (out.empty()) {
        std::cerr << "FATAL: golden file is empty: " << path << std::endl;
        std::exit(2);
    }
    return out;
}

// Load "a b c" triples per line (used for the segment table).
struct Row {
    std::vector<long long> fields;
};

inline std::vector<Row> load_rows(const std::string& path) {
    std::ifstream in(path.c_str());
    if (!in) {
        std::cerr << "FATAL: cannot open golden file " << path << std::endl;
        std::exit(2);
    }
    std::vector<Row> rows;
    std::string line;
    while (std::getline(in, line)) {
        if (line.empty()) continue;
        std::istringstream ss(line);
        Row row;
        long long value;
        while (ss >> value) row.fields.push_back(value);
        if (!row.fields.empty()) rows.push_back(row);
    }
    return rows;
}

class Checker {
public:
    explicit Checker(const char* name) : name_(name), failures_(0), checks_(0) {}

    template <typename A, typename B>
    void equal(const char* what, A actual, B expected, long long index = -1) {
        checks_++;
        if ((long long)actual == (long long)expected) return;
        if (failures_ < 10) {          // cap the log so a systemic break stays readable
            std::cerr << "  MISMATCH " << what;
            if (index >= 0) std::cerr << " at " << index;
            std::cerr << ": got " << (long long)actual
                      << ", expected " << (long long)expected << "\n";
        }
        failures_++;
    }

    template <typename T, typename U>
    void equal_vec(const char* what, const std::vector<T>& actual,
                   const std::vector<U>& expected) {
        if (actual.size() != expected.size()) {
            std::cerr << "  MISMATCH " << what << ": size " << actual.size()
                      << " vs expected " << expected.size() << "\n";
            failures_++;
            return;
        }
        for (size_t i = 0; i < actual.size(); i++) {
            equal(what, actual[i], expected[i], (long long)i);
        }
    }

    void note(const std::string& message) const {
        std::cout << "  " << message << "\n";
    }

    int finish() const {
        std::cout << "[" << name_ << "] " << checks_ << " comparisons, "
                  << failures_ << " mismatches -> "
                  << (failures_ == 0 ? "PASS" : "FAIL") << std::endl;
        return failures_ == 0 ? 0 : 1;
    }

    int failures() const { return failures_; }

private:
    const char* name_;
    long long failures_;
    long long checks_;
};

// TLAST must appear exactly once, on the final word, or the DMA either terminates the
// transfer early or never terminates it at all.
inline void check_tlast(Checker& check, const std::vector<bool>& last) {
    long long count = 0;
    for (size_t i = 0; i < last.size(); i++) {
        if (last[i]) count++;
    }
    check.equal("tlast_count", count, 1);
    if (!last.empty()) {
        check.equal("tlast_on_final_word", last.back() ? 1 : 0, 1);
    }
}

}  // namespace tb

#endif  // TB_UTIL_H
