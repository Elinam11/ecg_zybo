// Bare-metal Cortex-A9 control program for the ECG compression system.
//
// Per run:
//   1. arm the DMA to receive the compressed frame into DDR
//   2. start the PL core and time it with the A9 global timer
//   3. read the status registers, build the 128-bit frame header from them
//   4. push the transport preamble, the header and the payload out the PS UART
//
// The frame leaves on the PS UART, which is the same micro-USB console the log text
// uses, so a board test needs one cable instead of a second adapter wired to Pmod JE1.
// The cost is that text and binary share one link: everything human-readable must be
// printed either side of the frame, never during it, or the host loses framing.
//
// The header is assembled here rather than in the PL because its last word is a
// checksum over the whole payload, and a streaming master cannot revisit a header it
// has already transmitted. Buffering the payload on chip to avoid that is not an
// option: the sample store already occupies most of the device's block RAM.

#include <stdio.h>
#include <string.h>

#include "xil_cache.h"
#include "xil_printf.h"
#include "xil_types.h"
#include "xaxidma.h"
#include "xtime_l.h"

#include "ecg_hw.h"

// The BSP's raw stdout byte sink, which for this platform is the PS UART. Declared here
// because the header that exposes it moved between tool releases, and an implicit
// declaration is an error under the default BSP compiler flags.
void outbyte(char8 c);

static XAxiDma dma;

// ---------------------------------------------------------------------------
// Core control
// ---------------------------------------------------------------------------

static void ecg_core_start(uint32_t n_samples) {
    ecg_write32(ECG_CTRL_BASE, XECG_TOP_CTRL_ADDR_N_SAMPLES_DATA, n_samples);
    ecg_write32(ECG_CTRL_BASE, XECG_TOP_CTRL_ADDR_AP_CTRL, ECG_AP_START);
}

static int ecg_core_busy(void) {
    return (ecg_read32(ECG_CTRL_BASE, XECG_TOP_CTRL_ADDR_AP_CTRL) & ECG_AP_DONE) == 0;
}

typedef struct {
    uint32_t word_count;
    uint32_t qrs_count;
    uint32_t checksum;
    uint32_t peak_count;
    uint32_t saturated;
} ecg_run_status;

static void ecg_core_read_status(ecg_run_status* s) {
    const uint32_t base = XECG_TOP_CTRL_ADDR_STATUS_DATA;
    s->word_count = ecg_read32(ECG_CTRL_BASE, base + ECG_STATUS_WORD_COUNT);
    s->qrs_count  = ecg_read32(ECG_CTRL_BASE, base + ECG_STATUS_QRS_COUNT);
    s->checksum   = ecg_read32(ECG_CTRL_BASE, base + ECG_STATUS_CHECKSUM);
    s->peak_count = ecg_read32(ECG_CTRL_BASE, base + ECG_STATUS_PEAK_COUNT);
    s->saturated  = ecg_read32(ECG_CTRL_BASE, base + ECG_STATUS_SATURATED);
}

// ---------------------------------------------------------------------------
// UART transmission
// ---------------------------------------------------------------------------

// Raw bytes straight out of outbyte(), which blocks while the PS UART transmit FIFO is
// full and so cannot drop a byte. printf() and friends are deliberately not used here:
// they buffer, and a stdio layer is free to translate a 0x0A in the payload into CRLF,
// which would corrupt the frame.
static void uart_send_blocking(const uint8_t* data, uint32_t length) {
    for (uint32_t i = 0; i < length; i++) {
        outbyte((char8)data[i]);
    }
}

// Every multi-byte value on the wire is big-endian, matching the order the packer
// shifts bits into a word, so the host can treat the stream as a byte sequence.
static void uart_send_word(uint32_t value) {
    const uint8_t bytes[4] = {
        (uint8_t)(value >> 24), (uint8_t)(value >> 16),
        (uint8_t)(value >> 8),  (uint8_t)value,
    };
    uart_send_blocking(bytes, 4);
}

// ---------------------------------------------------------------------------
// One compression run
// ---------------------------------------------------------------------------

static int run_once(uint32_t n_samples) {
    uint8_t* const buffer = (uint8_t*)(uintptr_t)ECG_FRAME_BUFFER_ADDR;

    // The DMA writes straight to DDR, so anything the CPU has cached for this region
    // is stale the moment the transfer starts.
    Xil_DCacheFlushRange((INTPTR)buffer, ECG_FRAME_BUFFER_BYTES);

    int status = XAxiDma_SimpleTransfer(&dma, (UINTPTR)buffer,
                                        ECG_FRAME_BUFFER_BYTES, XAXIDMA_DEVICE_TO_DMA);
    if (status != XST_SUCCESS) {
        xil_printf("ERROR: could not arm the S2MM channel (%d)\r\n", status);
        return XST_FAILURE;
    }

    XTime t_start;
    XTime_GetTime(&t_start);
    ecg_core_start(n_samples);
    while (ecg_core_busy()) {
        // Polling rather than waiting on the interrupt: the run is a few milliseconds
        // and an idle loop here cannot perturb the measurement.
    }
    XTime t_done;
    XTime_GetTime(&t_done);

    // The core is done producing, but the last words may still be in flight.
    while (XAxiDma_Busy(&dma, XAXIDMA_DEVICE_TO_DMA)) {
    }

    ecg_run_status s;
    ecg_core_read_status(&s);

    Xil_DCacheInvalidateRange((INTPTR)buffer, ECG_FRAME_BUFFER_BYTES);

    // The global timer runs at half the CPU frequency; convert to PL clocks so the
    // number is comparable with the HLS latency report.
    const double seconds = (double)(t_done - t_start) / (double)COUNTS_PER_SECOND;
    const uint32_t pl_cycles = (uint32_t)(seconds * (double)ECG_CLK_FREQ_HZ);

    const uint32_t frame_words = ECG_HEADER_WORDS + s.word_count;
    const uint32_t payload_bytes = s.word_count * 4u;

    if (payload_bytes > ECG_FRAME_BUFFER_BYTES) {
        xil_printf("ERROR: payload %u bytes exceeds the %u byte buffer\r\n",
                   (unsigned)payload_bytes, (unsigned)ECG_FRAME_BUFFER_BYTES);
        return XST_FAILURE;
    }

    // Every human-readable line for this run goes out before the frame does. The host
    // resynchronises on the sync word, so text ahead of the frame is harmless.
    xil_printf("run: samples=%u beats=%u peaks=%u payload=%u words sat=%u\r\n",
               (unsigned)n_samples, (unsigned)s.qrs_count, (unsigned)s.peak_count,
               (unsigned)s.word_count, (unsigned)s.saturated);
    xil_printf("     latency %u PL cycles (%u us)\r\n",
               (unsigned)pl_cycles, (unsigned)(seconds * 1e6));
    xil_printf("     sending %u bytes\r\n",
               (unsigned)((ECG_UART_PREAMBLE + frame_words) * 4u));

    // ------------------------------------------------------------------
    // Quiet window: nothing but frame bytes may reach the UART from here
    // to the end of the payload. A single stray log line lands inside the
    // frame the host is counting out, and the capture is unrecoverable.
    // ------------------------------------------------------------------

    // ---- Transport preamble: sync, length, measured latency ----
    uart_send_word(ECG_UART_SYNC);
    uart_send_word(frame_words);
    uart_send_word(pl_cycles);

    // ---- Frame header, built from the status registers ----
    uart_send_word(ECG_MAGIC_WORD);
    uart_send_word(n_samples);
    uart_send_word(((uint32_t)ECG_FS & 0xFFFFu) | ((s.qrs_count & 0xFFFFu) << 16));
    uart_send_word(s.checksum);

    // ---- Payload words, big-endian on the wire (same as uart_send_word) ----
    // The DMA buffer is native little-endian in DDR. Sending it with
    // uart_send_blocking() put LE bytes on the wire while the header stayed BE,
    // so the host saw a checksum mismatch and a corrupted bitstream.
    {
        const uint32_t *words = (const uint32_t *)(uintptr_t)buffer;
        for (uint32_t i = 0; i < s.word_count; i++) {
            uart_send_word(words[i]);
        }
    }

    // ---- End of quiet window ----
    return XST_SUCCESS;
}

// ---------------------------------------------------------------------------

static int init_hardware(void) {
    XAxiDma_Config* dma_cfg = XAxiDma_LookupConfig(ECG_DMA_DEVICE_ID);
    if (dma_cfg == NULL) {
        xil_printf("ERROR: no DMA config for device %d\r\n", ECG_DMA_DEVICE_ID);
        return XST_FAILURE;
    }
    if (XAxiDma_CfgInitialize(&dma, dma_cfg) != XST_SUCCESS) {
        xil_printf("ERROR: DMA initialisation failed\r\n");
        return XST_FAILURE;
    }
    if (XAxiDma_HasSg(&dma)) {
        xil_printf("ERROR: DMA is configured for scatter-gather; "
                   "the block design must use simple mode\r\n");
        return XST_FAILURE;
    }
    XAxiDma_IntrDisable(&dma, XAXIDMA_IRQ_ALL_MASK, XAXIDMA_DEVICE_TO_DMA);

    // No UART setup: the PS UART is already configured by ps7_init at the baud rate the
    // BSP's stdout uses, and the PL UARTLite still in the block design is unused.
    return XST_SUCCESS;
}

int main(void) {
    Xil_DCacheEnable();
    Xil_ICacheEnable();

    xil_printf("\r\n=== FPGA adaptive ECG compression ===\r\n");
    xil_printf("core @ 0x%08X, sample store @ 0x%08X, %u samples\r\n",
               ECG_CTRL_BASE, ECG_BRAM_BASE, ECG_N_SAMPLES);

    if (init_hardware() != XST_SUCCESS) {
        return XST_FAILURE;
    }

    // The sample store is initialised from the .coe at configuration time, so a run can
    // start immediately. Loading a different record at run time means writing 32-bit
    // words to ECG_BRAM_BASE (two samples per word, first sample in the low half)
    // before calling run_once.
    const int status = run_once(ECG_N_SAMPLES);

    // The payload ends without a newline, so lead with one to keep this line readable in
    // a terminal. The host has already stopped reading by now and ignores it.
    xil_printf(status == XST_SUCCESS ? "\r\ndone\r\n" : "\r\nFAILED\r\n");
    Xil_DCacheDisable();
    return status;
}
