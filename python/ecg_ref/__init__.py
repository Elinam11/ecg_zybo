"""Golden reference model for the FPGA adaptive ECG compression system.

This package is the single source of truth for the algorithm. The HLS implementation
is verified bit-exactly against vectors emitted from here.
"""

__all__ = [
    "config",
    "fixedpoint",
    "preprocess",
    "detect",
    "lossless",
    "lossy",
    "packer",
    "pipeline",
    "metrics",
    "pan_tompkins",
]
