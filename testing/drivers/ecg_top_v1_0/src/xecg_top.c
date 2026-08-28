// ==============================================================
// Vitis HLS - High-Level Synthesis from C, C++ and OpenCL v2023.2 (64-bit)
// Tool Version Limit: 2023.10
// Copyright 1986-2022 Xilinx, Inc. All Rights Reserved.
// Copyright 2022-2023 Advanced Micro Devices, Inc. All Rights Reserved.
// 
// ==============================================================
/***************************** Include Files *********************************/
#include "xecg_top.h"

/************************** Function Implementation *************************/
#ifndef __linux__
int XEcg_top_CfgInitialize(XEcg_top *InstancePtr, XEcg_top_Config *ConfigPtr) {
    Xil_AssertNonvoid(InstancePtr != NULL);
    Xil_AssertNonvoid(ConfigPtr != NULL);

    InstancePtr->Ctrl_BaseAddress = ConfigPtr->Ctrl_BaseAddress;
    InstancePtr->IsReady = XIL_COMPONENT_IS_READY;

    return XST_SUCCESS;
}
#endif

void XEcg_top_Start(XEcg_top *InstancePtr) {
    u32 Data;

    Xil_AssertVoid(InstancePtr != NULL);
    Xil_AssertVoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    Data = XEcg_top_ReadReg(InstancePtr->Ctrl_BaseAddress, XECG_TOP_CTRL_ADDR_AP_CTRL) & 0x80;
    XEcg_top_WriteReg(InstancePtr->Ctrl_BaseAddress, XECG_TOP_CTRL_ADDR_AP_CTRL, Data | 0x01);
}

u32 XEcg_top_IsDone(XEcg_top *InstancePtr) {
    u32 Data;

    Xil_AssertNonvoid(InstancePtr != NULL);
    Xil_AssertNonvoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    Data = XEcg_top_ReadReg(InstancePtr->Ctrl_BaseAddress, XECG_TOP_CTRL_ADDR_AP_CTRL);
    return (Data >> 1) & 0x1;
}

u32 XEcg_top_IsIdle(XEcg_top *InstancePtr) {
    u32 Data;

    Xil_AssertNonvoid(InstancePtr != NULL);
    Xil_AssertNonvoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    Data = XEcg_top_ReadReg(InstancePtr->Ctrl_BaseAddress, XECG_TOP_CTRL_ADDR_AP_CTRL);
    return (Data >> 2) & 0x1;
}

u32 XEcg_top_IsReady(XEcg_top *InstancePtr) {
    u32 Data;

    Xil_AssertNonvoid(InstancePtr != NULL);
    Xil_AssertNonvoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    Data = XEcg_top_ReadReg(InstancePtr->Ctrl_BaseAddress, XECG_TOP_CTRL_ADDR_AP_CTRL);
    // check ap_start to see if the pcore is ready for next input
    return !(Data & 0x1);
}

void XEcg_top_EnableAutoRestart(XEcg_top *InstancePtr) {
    Xil_AssertVoid(InstancePtr != NULL);
    Xil_AssertVoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    XEcg_top_WriteReg(InstancePtr->Ctrl_BaseAddress, XECG_TOP_CTRL_ADDR_AP_CTRL, 0x80);
}

void XEcg_top_DisableAutoRestart(XEcg_top *InstancePtr) {
    Xil_AssertVoid(InstancePtr != NULL);
    Xil_AssertVoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    XEcg_top_WriteReg(InstancePtr->Ctrl_BaseAddress, XECG_TOP_CTRL_ADDR_AP_CTRL, 0);
}

void XEcg_top_Set_n_samples(XEcg_top *InstancePtr, u32 Data) {
    Xil_AssertVoid(InstancePtr != NULL);
    Xil_AssertVoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    XEcg_top_WriteReg(InstancePtr->Ctrl_BaseAddress, XECG_TOP_CTRL_ADDR_N_SAMPLES_DATA, Data);
}

u32 XEcg_top_Get_n_samples(XEcg_top *InstancePtr) {
    u32 Data;

    Xil_AssertNonvoid(InstancePtr != NULL);
    Xil_AssertNonvoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    Data = XEcg_top_ReadReg(InstancePtr->Ctrl_BaseAddress, XECG_TOP_CTRL_ADDR_N_SAMPLES_DATA);
    return Data;
}

XEcg_top_Status XEcg_top_Get_status(XEcg_top *InstancePtr) {
    XEcg_top_Status Data;

    Data.word_0 = XEcg_top_ReadReg(InstancePtr->Ctrl_BaseAddress, XECG_TOP_CTRL_ADDR_STATUS_DATA + 0);
    Data.word_1 = XEcg_top_ReadReg(InstancePtr->Ctrl_BaseAddress, XECG_TOP_CTRL_ADDR_STATUS_DATA + 4);
    Data.word_2 = XEcg_top_ReadReg(InstancePtr->Ctrl_BaseAddress, XECG_TOP_CTRL_ADDR_STATUS_DATA + 8);
    Data.word_3 = XEcg_top_ReadReg(InstancePtr->Ctrl_BaseAddress, XECG_TOP_CTRL_ADDR_STATUS_DATA + 12);
    Data.word_4 = XEcg_top_ReadReg(InstancePtr->Ctrl_BaseAddress, XECG_TOP_CTRL_ADDR_STATUS_DATA + 16);
    return Data;
}

u32 XEcg_top_Get_status_vld(XEcg_top *InstancePtr) {
    u32 Data;

    Xil_AssertNonvoid(InstancePtr != NULL);
    Xil_AssertNonvoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    Data = XEcg_top_ReadReg(InstancePtr->Ctrl_BaseAddress, XECG_TOP_CTRL_ADDR_STATUS_CTRL);
    return Data & 0x1;
}

void XEcg_top_InterruptGlobalEnable(XEcg_top *InstancePtr) {
    Xil_AssertVoid(InstancePtr != NULL);
    Xil_AssertVoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    XEcg_top_WriteReg(InstancePtr->Ctrl_BaseAddress, XECG_TOP_CTRL_ADDR_GIE, 1);
}

void XEcg_top_InterruptGlobalDisable(XEcg_top *InstancePtr) {
    Xil_AssertVoid(InstancePtr != NULL);
    Xil_AssertVoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    XEcg_top_WriteReg(InstancePtr->Ctrl_BaseAddress, XECG_TOP_CTRL_ADDR_GIE, 0);
}

void XEcg_top_InterruptEnable(XEcg_top *InstancePtr, u32 Mask) {
    u32 Register;

    Xil_AssertVoid(InstancePtr != NULL);
    Xil_AssertVoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    Register =  XEcg_top_ReadReg(InstancePtr->Ctrl_BaseAddress, XECG_TOP_CTRL_ADDR_IER);
    XEcg_top_WriteReg(InstancePtr->Ctrl_BaseAddress, XECG_TOP_CTRL_ADDR_IER, Register | Mask);
}

void XEcg_top_InterruptDisable(XEcg_top *InstancePtr, u32 Mask) {
    u32 Register;

    Xil_AssertVoid(InstancePtr != NULL);
    Xil_AssertVoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    Register =  XEcg_top_ReadReg(InstancePtr->Ctrl_BaseAddress, XECG_TOP_CTRL_ADDR_IER);
    XEcg_top_WriteReg(InstancePtr->Ctrl_BaseAddress, XECG_TOP_CTRL_ADDR_IER, Register & (~Mask));
}

void XEcg_top_InterruptClear(XEcg_top *InstancePtr, u32 Mask) {
    Xil_AssertVoid(InstancePtr != NULL);
    Xil_AssertVoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    XEcg_top_WriteReg(InstancePtr->Ctrl_BaseAddress, XECG_TOP_CTRL_ADDR_ISR, Mask);
}

u32 XEcg_top_InterruptGetEnabled(XEcg_top *InstancePtr) {
    Xil_AssertNonvoid(InstancePtr != NULL);
    Xil_AssertNonvoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    return XEcg_top_ReadReg(InstancePtr->Ctrl_BaseAddress, XECG_TOP_CTRL_ADDR_IER);
}

u32 XEcg_top_InterruptGetStatus(XEcg_top *InstancePtr) {
    Xil_AssertNonvoid(InstancePtr != NULL);
    Xil_AssertNonvoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    return XEcg_top_ReadReg(InstancePtr->Ctrl_BaseAddress, XECG_TOP_CTRL_ADDR_ISR);
}

