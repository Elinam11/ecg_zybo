import numpy as np
import pywt
from ecg_ref import lossy_bw

n = 1024
t = np.arange(n)
x = (200 * np.sin(2 * np.pi * t / 40) + 40 * np.sin(2 * np.pi * t / 11)).astype(float)
levels = 4
ours = lossy_bw.dwt_forward_lin(x, levels)
print("ours lens", {k: len(v) for k, v in ours.items()})
for mode in ["reflect", "symmetric", "periodization", "periodic", "smooth"]:
    py = pywt.wavedec(x, "bior4.4", level=levels, mode=mode)
    print("mode", mode, "lens py", [len(a) for a in py])
    for j, name in [(0, f"cA{levels}"), (1, "cD4"), (2, "cD3"), (3, "cD2"), (4, "cD1")]:
        a = np.asarray(ours[name], float)
        b = np.asarray(py[j], float)
        best = None
        for sh in range(-8, 9):
            if sh >= 0:
                aa, bb = a[16:-16], b[16 + sh:16 + sh + len(a[16:-16])]
            else:
                aa, bb = a[16 - sh:-16], b[16:16 + len(a[16 - sh:-16])]
            m = min(len(aa), len(bb))
            if m < 8 or np.std(aa[:m]) == 0 or np.std(bb[:m]) == 0:
                continue
            c = float(np.corrcoef(aa[:m], bb[:m])[0, 1])
            if best is None or abs(c) > abs(best[0]):
                s = float(np.dot(aa[:m], bb[:m]) / np.dot(bb[:m], bb[:m]))
                best = (c, sh, s, m)
        print(f"  {name}: best {best}")

print("energy x", float(np.sum(x * x)),
      "sum bands", sum(float(np.sum(np.square(v))) for v in ours.values()))
print("linear rt maxerr", np.max(np.abs(lossy_bw.dwt_inverse_lin(ours, levels) - x)))
