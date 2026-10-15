import numpy as np
import pywt
from ecg_ref import lossy_bw, config

n = 1024
t = np.arange(n)
x = np.sin(2 * np.pi * t / 32.0)
ex = float(np.sum(x * x))

bands = lossy_bw.dwt_forward_lin(x, 5)
ec = sum(float(np.sum(np.square(v))) for v in bands.values())
print("ours quantized-const energy", ec / ex, {k: float(np.sum(np.square(v))) / ex for k, v in bands.items()})

# exact constants
lossy_bw.LIFT_ALPHA = config.BW_LIFT_ALPHA
lossy_bw.LIFT_BETA = config.BW_LIFT_BETA
lossy_bw.LIFT_GAMMA = config.BW_LIFT_GAMMA
lossy_bw.LIFT_DELTA = config.BW_LIFT_DELTA
lossy_bw.SCALE_LOW = config.BW_SCALE_LOW
lossy_bw.SCALE_HIGH = config.BW_SCALE_HIGH
# need to reload functions... they close over module-level names, so assignment works
bands2 = lossy_bw.dwt_forward_lin(x, 5)
ec2 = sum(float(np.sum(np.square(v))) for v in bands2.values())
print("ours exact-const energy", ec2 / ex)

py = pywt.wavedec(x, "bior4.4", level=5, mode="periodization")
ecp = sum(float(np.sum(np.square(a))) for a in py)
print("pywt periodization energy", ecp / ex, [float(np.sum(np.square(a))) / ex for a in py])

py2 = pywt.wavedec(x, "bior4.4", level=5, mode="reflect")
ecp2 = sum(float(np.sum(np.square(a))) for a in py2)
print("pywt reflect energy", ecp2 / ex)

# JPEG2000-style: skip pywt extra scale, only /K and -K
print("SCALE_LOW", config.BW_SCALE_LOW, "SCALE_HIGH", config.BW_SCALE_HIGH)
print("sqrt2/K", np.sqrt(2) / config.BW_LIFT_K, "-K/sqrt2", -config.BW_LIFT_K / np.sqrt(2))
