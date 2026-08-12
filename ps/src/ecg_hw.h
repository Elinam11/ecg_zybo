// Hardware map for the ECG compression system, shared by every PS source file.
//
// The base addresses match the explicit `assign_bd_address` calls in
// vivado/create_bd.tcl. They are asserted against xparameters.h below, so a change to
// the block design that is not mirrored here fails to compile rather than producing
// firmware that quietly talks to the wrong peripheral.
#ifndef ECG_HW_H
#define ECG_HW_H

#include <stdint.h>

#include "xparameters.h"

// Constants shared with the Python reference model and the HLS core. Add
// <repo>/hls/src to the BSP include path so this resolves; it is generated from
// config.py and is the only place these values are defined.
#include "ecg_constants.h"

// ---------------------------------------------------------------------------
// Address map
// ---------------------------------------------------------------------------
#define ECG_BRAM_BASE     0x40000000u   // sample store, port A (32-bit words)
#define ECG_BRAM_SIZE     0x00040000u
#define ECG_CTRL_BASE     0x43C00000u   // ecg_top AXI4-Lite control

// The DMA and UART are addressed through their BSP drivers, so they only need device
// IDs. Everything else is a raw register access.
#define ECG_DMA_DEVICE_ID    XPAR_AXIDMA_0_DEVICE_ID
#define ECG_UART_DEVICE_ID   XPAR_UARTLITE_0_DEVICE_ID

// ---------------------------------------------------------------------------
// ecg_top AXI4-Lite register map
// ---------------------------------------------------------------------------
//
// Vitis HLS generates the authoritative version of this map as xecg_top_hw.h inside the
// exported IP, and the driver it ships with it. Prefer that header when it is on the
// include path; the fallback offsets below are the standard HLS layout (control block,
// then arguments in declaration order) and let this file compile before the IP has been
// exported.
#if defined(__has_include)
#  if __has_include("xecg_top_hw.h")
#    include "xecg_top_hw.h"
#    define ECG_HAVE_GENERATED_MAP 1
#  endif
#endif

#ifndef ECG_HAVE_GENERATED_MAP
#define XECG_TOP_CTRL_ADDR_AP_CTRL         0x00
#define XECG_TOP_CTRL_ADDR_GIE             0x04
#define XECG_TOP_CTRL_ADDR_IER             0x08
#define XECG_TOP_CTRL_ADDR_ISR             0x0C
#define XECG_TOP_CTRL_ADDR_N_SAMPLES_DATA  0x10
#define XECG_TOP_CTRL_ADDR_STATUS_DATA     0x18   // word_count
#endif

// Offsets of the ecg_status members from the start of the status register block.
// The struct is five 32-bit words in declaration order (see hls/src/ecg_types.h).
#define ECG_STATUS_WORD_COUNT   0x00
#define ECG_STATUS_QRS_COUNT    0x04
#define ECG_STATUS_CHECKSUM     0x08
#define ECG_STATUS_PEAK_COUNT   0x0C
#define ECG_STATUS_SATURATED    0x10

// ap_ctrl bits
#define ECG_AP_START    (1u << 0)
#define ECG_AP_DONE     (1u << 1)
#define ECG_AP_IDLE     (1u << 2)
#define ECG_AP_READY    (1u << 3)

// ---------------------------------------------------------------------------
// DDR staging buffer for the compressed frame
// ---------------------------------------------------------------------------
//
// Sized for the worst case the reference model allows: at the CR > 5 target the payload
// cannot exceed 108,000 x 16 / 5 bits, and 256 KB leaves better than 5x headroom for a
// pathological record that compresses poorly.
#define ECG_FRAME_BUFFER_ADDR   0x10000000u
#define ECG_FRAME_BUFFER_BYTES  (256u * 1024u)

// ---------------------------------------------------------------------------
// Register helpers
// ---------------------------------------------------------------------------
static inline void ecg_write32(uint32_t base, uint32_t offset, uint32_t value) {
    *(volatile uint32_t*)(uintptr_t)(base + offset) = value;
}

static inline uint32_t ecg_read32(uint32_t base, uint32_t offset) {
    return *(volatile uint32_t*)(uintptr_t)(base + offset);
}

#endif  // ECG_HW_H
