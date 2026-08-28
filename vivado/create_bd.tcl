# Block design for the ECG compression system on the Zybo Z7-10.
#
# Sourced by build_vivado.tcl; assumes a project already exists and the exported HLS IP
# repository has been added.
#
# ---------------------------------------------------------------------------
# Architecture
# ---------------------------------------------------------------------------
#
#   PS7 ──M_AXI_GP0──► interconnect ──► axi_bram_ctrl ──┐
#                                  ├──► ecg_top (ctrl)  │ port A (32-bit, PS writes)
#                                  ├──► axi_dma (ctrl)  │
#                                  └──► axi_uartlite    │
#                                                       ▼
#                                            blk_mem_gen (true dual port)
#                                                       │ port B (32-bit, core reads)
#                                            ecg_top ◄──┘
#                                                │ AXI4-Stream + TLAST
#                                                ▼
#                                          axi_dma S2MM ──► S_AXI_HP0 ──► DDR
#
# The sample store is dual-ported on purpose. Port B feeds the core; port A lets the PS
# overwrite the record at run time, so swapping datasets needs neither re-synthesis nor
# a bitstream patch. The .coe still initialises it, so a freshly programmed board runs
# the default record with no software involvement at all.
#
# Both ports are 32-bit: Vivado 2024.2 blk_mem_gen rejects a 16-bit port B when port A
# is 32-bit True Dual Port. Samples are packed two-per-word (matching gen_coe.py /
# *_x32.coe); the HLS core unpacks with ecg_bram_read().
#
# Compressed output goes to DDR by DMA rather than straight to the UART. At 115200 baud
# the UART is roughly four orders of magnitude slower than the core, so streaming
# directly would back-pressure the pipeline and make the measured latency a property of
# the serial link instead of the compression engine.

set ::BD_NAME "ecg_system"
set BD_NAME $::BD_NAME

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Instantiate an IP by VLNV prefix, resolving whatever version this Vivado ships.
# Hard-coding versions is the usual reason a block-design script stops working after a
# tool update.
proc ecg_add_ip {name vlnv_prefix} {
    set candidates [lsort [get_ipdefs -all -filter "VLNV =~ ${vlnv_prefix}:*"]]
    if {[llength $candidates] == 0} {
        error "IP not found in the catalog: $vlnv_prefix"
    }
    set vlnv [lindex $candidates end]
    puts "  instantiating $name ($vlnv)"
    return [create_bd_cell -type ip -vlnv $vlnv $name]
}

proc ecg_connect {a b} {
    set pa [get_bd_pins $a]
    set pb [get_bd_pins $b]
    set na [get_bd_nets -quiet -of_objects $pa]
    set nb [get_bd_nets -quiet -of_objects $pb]
    # Vivado 2024.2 errors on reconnecting an already-wired pin (BD 5-4 / BD 41-395),
    # including when the early reset wiring and the later clock sweep both target the
    # same pin, or when an AXI connect auto-propagates a clock.
    if {$nb ne ""} {
        if {$na ne "" && $na eq $nb} {
            return
        }
        error "Cannot connect $a -> $b: $b is already on net $nb (source is on $na)"
    }
    connect_bd_net $pa $pb
}

# ---------------------------------------------------------------------------
# Create the design
# ---------------------------------------------------------------------------
create_bd_design $BD_NAME
current_bd_design $BD_NAME

# ---- Zynq PS ----
ecg_add_ip processing_system7_0 xilinx.com:ip:processing_system7

set board [get_property BOARD_PART [current_project]]
if {$board ne ""} {
    # The board preset carries the Zybo's DDR3 timing, MIO assignment and clock
    # sources. Getting those right by hand is not realistic, so a missing board file
    # is a hard error rather than something to paper over.
    apply_bd_automation -rule xilinx.com:bd_rule:processing_system7 \
        -config {make_external "FIXED_IO, DDR" apply_board_preset "1" \
                 Master "Disable" Slave "Disable"} \
        [get_bd_cells processing_system7_0]
} else {
    error "No board part set. Install the Digilent board files and re-run with\
           -tclargs -board digilentinc.com:zybo-z7-10:part0:1.0, or the PS DDR\
           configuration will be wrong."
}

set_property -dict [list \
    CONFIG.PCW_USE_M_AXI_GP0 {1} \
    CONFIG.PCW_USE_S_AXI_HP0 {1} \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ {100} \
    CONFIG.PCW_EN_CLK0_PORT {1} \
    CONFIG.PCW_IRQ_F2P_INTR {1} \
    CONFIG.PCW_USE_FABRIC_INTERRUPT {1} \
] [get_bd_cells processing_system7_0]

# ---- Reset ----
ecg_add_ip proc_sys_reset_0 xilinx.com:ip:proc_sys_reset
ecg_connect processing_system7_0/FCLK_CLK0 proc_sys_reset_0/slowest_sync_clk
ecg_connect processing_system7_0/FCLK_RESET0_N proc_sys_reset_0/ext_reset_in

# ---- Sample store: true dual-port BRAM, symmetric 32-bit ports ----
#
# Port A serves the AXI BRAM controller; port B serves the HLS core. Both ports are
# 32-bit wide (Vivado 2024.2 rejects asymmetric 16-bit port B on TDP when port A is
# 32-bit). Initialisation uses the packed *_x32.coe: sample 2i in [15:0], 2i+1 in
# [31:16]. The core unpacks half-words in software/HLS.
#
# Enable_32bit_Address MUST be true. AXI BRAM ctrl and Vitis HLS both present BYTE
# addresses on the BRAM interface. With it false, those bytes are used as word
# indices, so masters only ever see words 0,4,8,... (record 100 then detects ~877
# peaks instead of 368, and PRD explodes).
ecg_add_ip blk_mem_gen_0 xilinx.com:ip:blk_mem_gen
set_property -dict [list \
    CONFIG.Memory_Type {True_Dual_Port_RAM} \
    CONFIG.Enable_32bit_Address {true} \
    CONFIG.Use_Byte_Write_Enable {true} \
    CONFIG.Byte_Size {8} \
    CONFIG.Write_Width_A {32} \
    CONFIG.Write_Depth_A {54000} \
    CONFIG.Read_Width_A {32} \
    CONFIG.Operating_Mode_A {WRITE_FIRST} \
    CONFIG.Write_Width_B {32} \
    CONFIG.Read_Width_B {32} \
    CONFIG.Operating_Mode_B {READ_FIRST} \
    CONFIG.Enable_B {Use_ENB_Pin} \
    CONFIG.Port_B_Clock {100} \
    CONFIG.Port_B_Write_Rate {0} \
    CONFIG.Port_B_Enable_Rate {100} \
    CONFIG.Register_PortA_Output_of_Memory_Primitives {false} \
    CONFIG.Register_PortB_Output_of_Memory_Primitives {false} \
    CONFIG.Load_Init_File {true} \
    CONFIG.Coe_File $::ECG_COE_FILE \
    CONFIG.Fill_Remaining_Memory_Locations {true} \
    CONFIG.Remaining_Memory_Locations {0} \
] [get_bd_cells blk_mem_gen_0]

ecg_add_ip axi_bram_ctrl_0 xilinx.com:ip:axi_bram_ctrl
set_property -dict [list \
    CONFIG.DATA_WIDTH {32} \
    CONFIG.SINGLE_PORT_BRAM {1} \
    CONFIG.ECC_TYPE {0} \
] [get_bd_cells axi_bram_ctrl_0]

connect_bd_intf_net [get_bd_intf_pins axi_bram_ctrl_0/BRAM_PORTA] \
                    [get_bd_intf_pins blk_mem_gen_0/BRAM_PORTA]

# ---- The compression core ----
ecg_add_ip ecg_top_0 capstone:ecg:ecg_top
connect_bd_intf_net [get_bd_intf_pins ecg_top_0/ecg_bram_PORTA] \
                    [get_bd_intf_pins blk_mem_gen_0/BRAM_PORTB]
# HLS must stay ram_1p (only PORTA). Csynth used to emit a second BRAM port for
# the QRS k_scan pipeline; that pin has nowhere to connect on this TDP memory
# (port A is already the AXI controller).

# ---- DMA: stream to DDR ----
# Simple (non scatter-gather) S2MM only. The core produces one buffer per run and
# terminates it with TLAST, which is exactly what simple mode expects; scatter-gather
# would add descriptor management for no benefit.
ecg_add_ip axi_dma_0 xilinx.com:ip:axi_dma
set_property -dict [list \
    CONFIG.c_include_sg {0} \
    CONFIG.c_sg_include_stscntrl_strm {0} \
    CONFIG.c_include_mm2s {0} \
    CONFIG.c_include_s2mm {1} \
    CONFIG.c_s2mm_burst_size {16} \
    CONFIG.c_m_axi_s2mm_data_width {32} \
    CONFIG.c_sg_length_width {26} \
] [get_bd_cells axi_dma_0]

connect_bd_intf_net [get_bd_intf_pins ecg_top_0/out_stream] \
                    [get_bd_intf_pins axi_dma_0/S_AXIS_S2MM]

# ---- UART: compressed frame out to the host ----
# PL-side UARTLite on Pmod JE1 (`tx_line`), per the board master XDC. The PS console
# UART stays free for human-readable logging.
ecg_add_ip axi_uartlite_0 xilinx.com:ip:axi_uartlite
set_property -dict [list \
    CONFIG.C_BAUDRATE {115200} \
    CONFIG.C_DATA_BITS {8} \
    CONFIG.C_USE_PARITY {0} \
] [get_bd_cells axi_uartlite_0]

# Nothing is ever received, so RX is held at the idle level rather than burning a pin.
ecg_add_ip uart_rx_idle_0 xilinx.com:ip:xlconstant
set_property -dict [list CONFIG.CONST_WIDTH {1} CONFIG.CONST_VAL {1}] \
    [get_bd_cells uart_rx_idle_0]
ecg_connect uart_rx_idle_0/dout axi_uartlite_0/rx

make_bd_pins_external -name tx_line [get_bd_pins axi_uartlite_0/tx]

# ---- Interconnect: PS control path ----
ecg_add_ip smartconnect_ctrl xilinx.com:ip:smartconnect
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {4}] \
    [get_bd_cells smartconnect_ctrl]

connect_bd_intf_net [get_bd_intf_pins processing_system7_0/M_AXI_GP0] \
                    [get_bd_intf_pins smartconnect_ctrl/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins smartconnect_ctrl/M00_AXI] \
                    [get_bd_intf_pins ecg_top_0/s_axi_ctrl]
connect_bd_intf_net [get_bd_intf_pins smartconnect_ctrl/M01_AXI] \
                    [get_bd_intf_pins axi_dma_0/S_AXI_LITE]
connect_bd_intf_net [get_bd_intf_pins smartconnect_ctrl/M02_AXI] \
                    [get_bd_intf_pins axi_bram_ctrl_0/S_AXI]
connect_bd_intf_net [get_bd_intf_pins smartconnect_ctrl/M03_AXI] \
                    [get_bd_intf_pins axi_uartlite_0/S_AXI]

# ---- Interconnect: DMA write path into DDR ----
ecg_add_ip smartconnect_hp xilinx.com:ip:smartconnect
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {1}] \
    [get_bd_cells smartconnect_hp]
connect_bd_intf_net [get_bd_intf_pins axi_dma_0/M_AXI_S2MM] \
                    [get_bd_intf_pins smartconnect_hp/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins smartconnect_hp/M00_AXI] \
                    [get_bd_intf_pins processing_system7_0/S_AXI_HP0]

# ---- Interrupts ----
# S2MM completion tells the PS the frame is fully in DDR; the core's own done flag is
# polled over AXI4-Lite.
ecg_add_ip irq_concat_0 xilinx.com:ip:xlconcat
set_property -dict [list CONFIG.NUM_PORTS {2}] [get_bd_cells irq_concat_0]
ecg_connect axi_dma_0/s2mm_introut irq_concat_0/In0
ecg_connect ecg_top_0/interrupt irq_concat_0/In1
ecg_connect irq_concat_0/dout processing_system7_0/IRQ_F2P

# ---------------------------------------------------------------------------
# Clocks and resets: everything in the PL runs on the single 100 MHz FCLK_CLK0
# ---------------------------------------------------------------------------
set clk_pins {
    axi_bram_ctrl_0/s_axi_aclk
    ecg_top_0/ap_clk
    axi_dma_0/s_axi_lite_aclk
    axi_dma_0/m_axi_s2mm_aclk
    axi_uartlite_0/s_axi_aclk
    smartconnect_ctrl/aclk
    smartconnect_hp/aclk
    processing_system7_0/M_AXI_GP0_ACLK
    processing_system7_0/S_AXI_HP0_ACLK
}
# proc_sys_reset_0/slowest_sync_clk is already tied to FCLK_CLK0 above.
foreach pin $clk_pins {
    ecg_connect processing_system7_0/FCLK_CLK0 $pin
}

set rst_pins {
    axi_bram_ctrl_0/s_axi_aresetn
    ecg_top_0/ap_rst_n
    axi_dma_0/axi_resetn
    axi_uartlite_0/s_axi_aresetn
    smartconnect_ctrl/aresetn
    smartconnect_hp/aresetn
}
foreach pin $rst_pins {
    ecg_connect proc_sys_reset_0/peripheral_aresetn $pin
}

# ---------------------------------------------------------------------------
# Address map
# ---------------------------------------------------------------------------
# Fixed rather than auto-assigned, because ps/src/ecg_hw.h hard-codes these and a
# silent re-assignment would produce a design that builds and then does nothing.
assign_bd_address -offset 0x40000000 -range 0x00040000 \
    [get_bd_addr_segs {axi_bram_ctrl_0/S_AXI/Mem0}]
assign_bd_address -offset 0x40400000 -range 0x00010000 \
    [get_bd_addr_segs {axi_dma_0/S_AXI_LITE/Reg}]
assign_bd_address -offset 0x42C00000 -range 0x00010000 \
    [get_bd_addr_segs {axi_uartlite_0/S_AXI/Reg}]
assign_bd_address -offset 0x43C00000 -range 0x00010000 \
    [get_bd_addr_segs {ecg_top_0/s_axi_ctrl/Reg}]

# The DMA writes into the PS DDR through HP0.
assign_bd_address [get_bd_addr_segs {processing_system7_0/S_AXI_HP0/HP0_DDR_LOWOCM}]

regenerate_bd_layout
validate_bd_design
save_bd_design

puts "Block design $BD_NAME created and validated."
