// ==============================================================
// Vitis HLS - High-Level Synthesis from C, C++ and OpenCL v2023.2 (64-bit)
// Tool Version Limit: 2023.10
// Copyright 1986-2022 Xilinx, Inc. All Rights Reserved.
// Copyright 2022-2023 Advanced Micro Devices, Inc. All Rights Reserved.
// 
// ==============================================================
#ifndef XECG_TOP_H
#define XECG_TOP_H

#ifdef __cplusplus
extern "C" {
#endif

/***************************** Include Files *********************************/
#ifndef __linux__
#include "xil_types.h"
#include "xil_assert.h"
#include "xstatus.h"
#include "xil_io.h"
#else
#include <stdint.h>
#include <assert.h>
#include <dirent.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include <stddef.h>
#endif
#include "xecg_top_hw.h"

/**************************** Type Definitions ******************************/
#ifdef __linux__
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
#else
typedef struct {
#ifdef SDT
    char *Name;
#else
    u16 DeviceId;
#endif
    u64 Ctrl_BaseAddress;
} XEcg_top_Config;
#endif

typedef struct {
    u64 Ctrl_BaseAddress;
    u32 IsReady;
} XEcg_top;

typedef u32 word_type;

typedef struct {
    u32 word_0;
    u32 word_1;
    u32 word_2;
    u32 word_3;
    u32 word_4;
} XEcg_top_Status;

/***************** Macros (Inline Functions) Definitions *********************/
#ifndef __linux__
#define XEcg_top_WriteReg(BaseAddress, RegOffset, Data) \
    Xil_Out32((BaseAddress) + (RegOffset), (u32)(Data))
#define XEcg_top_ReadReg(BaseAddress, RegOffset) \
    Xil_In32((BaseAddress) + (RegOffset))
#else
#define XEcg_top_WriteReg(BaseAddress, RegOffset, Data) \
    *(volatile u32*)((BaseAddress) + (RegOffset)) = (u32)(Data)
#define XEcg_top_ReadReg(BaseAddress, RegOffset) \
    *(volatile u32*)((BaseAddress) + (RegOffset))

#define Xil_AssertVoid(expr)    assert(expr)
#define Xil_AssertNonvoid(expr) assert(expr)

#define XST_SUCCESS             0
#define XST_DEVICE_NOT_FOUND    2
#define XST_OPEN_DEVICE_FAILED  3
#define XIL_COMPONENT_IS_READY  1
#endif

/************************** Function Prototypes *****************************/
#ifndef __linux__
#ifdef SDT
int XEcg_top_Initialize(XEcg_top *InstancePtr, UINTPTR BaseAddress);
XEcg_top_Config* XEcg_top_LookupConfig(UINTPTR BaseAddress);
#else
int XEcg_top_Initialize(XEcg_top *InstancePtr, u16 DeviceId);
XEcg_top_Config* XEcg_top_LookupConfig(u16 DeviceId);
#endif
int XEcg_top_CfgInitialize(XEcg_top *InstancePtr, XEcg_top_Config *ConfigPtr);
#else
int XEcg_top_Initialize(XEcg_top *InstancePtr, const char* InstanceName);
int XEcg_top_Release(XEcg_top *InstancePtr);
#endif

void XEcg_top_Start(XEcg_top *InstancePtr);
u32 XEcg_top_IsDone(XEcg_top *InstancePtr);
u32 XEcg_top_IsIdle(XEcg_top *InstancePtr);
u32 XEcg_top_IsReady(XEcg_top *InstancePtr);
void XEcg_top_EnableAutoRestart(XEcg_top *InstancePtr);
void XEcg_top_DisableAutoRestart(XEcg_top *InstancePtr);

void XEcg_top_Set_n_samples(XEcg_top *InstancePtr, u32 Data);
u32 XEcg_top_Get_n_samples(XEcg_top *InstancePtr);
XEcg_top_Status XEcg_top_Get_status(XEcg_top *InstancePtr);
u32 XEcg_top_Get_status_vld(XEcg_top *InstancePtr);

void XEcg_top_InterruptGlobalEnable(XEcg_top *InstancePtr);
void XEcg_top_InterruptGlobalDisable(XEcg_top *InstancePtr);
void XEcg_top_InterruptEnable(XEcg_top *InstancePtr, u32 Mask);
void XEcg_top_InterruptDisable(XEcg_top *InstancePtr, u32 Mask);
void XEcg_top_InterruptClear(XEcg_top *InstancePtr, u32 Mask);
u32 XEcg_top_InterruptGetEnabled(XEcg_top *InstancePtr);
u32 XEcg_top_InterruptGetStatus(XEcg_top *InstancePtr);

#ifdef __cplusplus
}
#endif

#endif
