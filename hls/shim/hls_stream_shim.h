// Minimal hls::stream<> stand-in for native g++ C-simulation.
//
// Used ONLY when ECG_NATIVE is defined, so that every module testbench can run and be
// diffed against the golden vectors on a machine without Vitis HLS installed. Vitis
// supplies the real hls_stream.h during csim/csynth/cosim, and the module sources are
// identical in both cases.
//
// This deliberately models the blocking FIFO semantics the design relies on (in-order,
// lossless, unbounded during simulation) and nothing else.
#ifndef HLS_STREAM_SHIM_H
#define HLS_STREAM_SHIM_H

#include <cstdio>
#include <cstdlib>
#include <deque>
#include <string>

namespace hls {

template <typename T>
class stream {
public:
    stream() : name_("stream") {}
    explicit stream(const char* name) : name_(name ? name : "stream") {}

    void write(const T& value) { queue_.push_back(value); }

    T read() {
        if (queue_.empty()) {
            std::fprintf(stderr, "FATAL: read from empty hls::stream '%s'\n",
                         name_.c_str());
            std::exit(2);
        }
        T value = queue_.front();
        queue_.pop_front();
        return value;
    }

    bool read_nb(T& value) {
        if (queue_.empty()) return false;
        value = queue_.front();
        queue_.pop_front();
        return true;
    }

    bool empty() const { return queue_.empty(); }
    bool full() const { return false; }
    size_t size() const { return queue_.size(); }

private:
    std::deque<T> queue_;
    std::string name_;
};

}  // namespace hls

#endif  // HLS_STREAM_SHIM_H
