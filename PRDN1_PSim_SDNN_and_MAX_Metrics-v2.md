# Diagnostic & Reconstruction Metrics: PRDN1, PSim SDNN, and MAX

This document details the theoretical background, mathematical equations, and calculation procedures for three primary ECG reconstruction quality metrics: **MAX** (Maximum Amplitude Error), **PRDN1** (Normalized Percentage Root Mean Square Difference), and **PSim SDNN** (Parameter Similarity of Standard Deviation of Normal-to-Normal Intervals) based on biomedical signal distortion assessment literature.

---

## 1. MAX (Maximum Amplitude Error)

### Background & Motivation
While integral and root-mean-square metrics (such as MSE or PRDN1) calculate average distortion across an entire signal window, they can obscure localized peak degradation or isolated high-magnitude artifacts. 

**MAX** measures the absolute worst-case point error between the original signal $x[n]$ and the reconstructed signal $\hat{x}[n]$. In biomedical literature, MAX boasts the highest Sensitivity-Variability Ratio (SVR) among standard time-domain metrics. It is particularly essential for detecting peak clipping in sharp R-wave peaks or boundary step-discontinuities (e.g., transitions between lossless QRS and lossy non-QRS processing regions).

---

### Mathematical Formulation

Given an original discrete ECG signal $x[n]$ and a reconstructed ECG signal $\hat{x}[n]$ of length $N$:

$$	ext{MAX} = \max_{1 \le n \le N} |x[n] - \hat{x}[n]|$$

#### Normalized MAX (Relative Peak Error):
To express MAX relative to the dynamic peak-to-peak amplitude range of the signal:
$$	ext{MAX}_{	ext{norm}} = rac{\max_{1 \le n \le N} |x[n] - \hat{x}[n]|}{\max(x[n]) - \min(x[n])} 	imes 100\%$$

---

### Step-by-Step Calculation Procedure
1. **Compute the absolute residual vector** across all $N$ samples: $e[n] = |x[n] - \hat{x}[n]|$.
2. **Find the maximum value** within the residual vector: $	ext{MAX} = \max(e[n])$.
3. *(Optional)* For scale-invariant reporting, divide by the original peak-to-peak amplitude range $[x_{\max} - x_{\min}]$ and multiply by $100$.

---

## 2. PRDN1 (Normalized Percentage Root Mean Square Difference)

### Background & Motivation
Standard Percentage Root Mean Square Difference ($	ext{PRD}$) and its variation $	ext{PRD1}$ are widely used to measure reconstruction fidelity in compressed electrocardiogram (ECG) signals. However, standard $	ext{PRD}$ is heavily biased by the mean (DC offset) of the original signal $x[n]$. If a baseline offset is added or subtracted, standard $	ext{PRD}$ values shift drastically without any actual change in signal morphology or noise structure.

$	ext{PRDN1}$ addresses this limitation by normalizing the error relative to the zero-mean signal, making it invariant to baseline shifts and offering a stable, universal metric for global amplitude distortion.

---

### Mathematical Formulation

Given an original discrete ECG signal $x[n]$ and a reconstructed ECG signal $\hat{x}[n]$ of length $N$, with mean value $ar{x} = rac{1}{N} \sum_{n=1}^{N} x[n]$:

#### Standard PRD:
$$	ext{PRD} = \sqrt{rac{\sum_{n=1}^{N} (x[n] - \hat{x}[n])^2}{\sum_{n=1}^{N} x[n]^2}} 	imes 100\%$$

#### PRDN1 (Mean-Removed / Normalized PRD):
$$	ext{PRDN1} = \sqrt{rac{\sum_{n=1}^{N} (x[n] - \hat{x}[n])^2}{\sum_{n=1}^{N} (x[n] - ar{x})^2}} 	imes 100\%$$

---

### Step-by-Step Calculation Procedure
1. **Compute the mean ($ar{x}$)** of the original signal $x[n]$ across the evaluation window of length $N$.
2. **Compute the pointwise residual (error)** between original and reconstructed signals: $e[n] = x[n] - \hat{x}[n]$.
3. **Square and sum the residuals** over $N$ samples: $	ext{SSE} = \sum_{n=1}^{N} e[n]^2$.
4. **Subtract the mean from the original signal** and square the centered values: $	ext{SST} = \sum_{n=1}^{N} (x[n] - ar{x})^2$.
5. **Compute the square root of the ratio** $rac{	ext{SSE}}{	ext{SST}}$ and multiply by $100$ to obtain the percentage error.

---

## 3. PSim SDNN (Parameter Similarity of SDNN)

### Background & Motivation
Sample-by-sample error metrics like MSE or PRDN1 measure geometric fidelity, but they do not directly reflect whether the compressed signal remains clinically useful for automated diagnostic algorithms.

**PSim SDNN** measures clinical diagnostic preservation by comparing a core Heart Rate Variability (HRV) parameter—the Standard Deviation of Normal-to-Normal NN/RR intervals ($	ext{SDNN}$)—calculated from the original signal versus the reconstructed signal. By focusing specifically on QRS peak timing rather than full-wave morphology delineation (such as complex P/T wave segmentation in WDD), PSim SDNN provides a computationally lightweight, robust metric for clinical usability.

---

### Mathematical Formulation

#### Step 1: SDNN Calculation
Let $RR_i = t_{R, i+1} - t_{R, i}$ be the duration of the $i$-th inter-beat interval detected from R-peak locations, where $M$ total intervals are present.

The mean inter-beat interval is:
$$\overline{RR} = rac{1}{M} \sum_{i=1}^{M} RR_i$$

The standard deviation ($	ext{SDNN}$) for the original signal $x$ and reconstructed signal $\hat{x}$ is calculated as:
$$	ext{SDNN}_x = \sqrt{rac{1}{M-1} \sum_{i=1}^{M} (RR_{x,i} - \overline{RR}_x)^2}$$
$$	ext{SDNN}_{\hat{x}} = \sqrt{rac{1}{M-1} \sum_{i=1}^{M} (RR_{\hat{x},i} - \overline{RR}_{\hat{x}})^2}$$

#### Step 2: Parameter Similarity (PSim)
$	ext{PSim SDNN}$ measures the percentage agreement or normalized similarity between the original parameter value $	ext{SDNN}_x$ and reconstructed value $	ext{SDNN}_{\hat{x}}$:

$$	ext{PSim SDNN} = \left( 1 - rac{|	ext{SDNN}_x - 	ext{SDNN}_{\hat{x}}|}{	ext{SDNN}_x} ight) 	imes 100\%$$

*Note: In formulations evaluating error/distortion rather than direct similarity, the relative parameter error ($	ext{PE}_{	ext{SDNN}}$) can also be expressed as:*
$$	ext{PE}_{	ext{SDNN}} = rac{|	ext{SDNN}_x - 	ext{SDNN}_{\hat{x}}|}{	ext{SDNN}_x} 	imes 100\%$$

---

### Step-by-Step Calculation Procedure
1. **Detect R-peaks** on both the original signal $x[n]$ and reconstructed signal $\hat{x}[n]$ using a QRS detector (e.g., Pan-Tompkins algorithm).
2. **Compute RR intervals** in milliseconds for both signals: $RR_i = t_{R, i+1} - t_{R, i}$.
3. **Calculate $	ext{SDNN}_x$ and $	ext{SDNN}_{\hat{x}}$** using sample standard deviation over the window.
4. **Compute Parameter Similarity ($	ext{PSim SDNN}$)** using the relative absolute difference equation above. A value near $100\%$ indicates perfect preservation of autonomic nervous system / HRV rhythm features.

---

## Summary Comparison

| Metric | Primary Purpose | Sensitivity & Constraints | Computational Complexity |
| :--- | :--- | :--- | :--- |
| **MAX** | Worst-case localized amplitude error | Highly sensitive to transient spikes, peak degradation, and boundary artifacts; scale-dependent unless normalized. | Extremely Low $\mathcal{O}(N)$ |
| **PRDN1** | Global morphology reconstruction error | Unbiased by baseline DC shifts; standardizes across different recording setups. | Very Low $\mathcal{O}(N)$ |
| **PSim SDNN** | Diagnostic / HRV feature preservation | Specifically sensitive to R-peak timing and jitter; robust against non-QRS waveform noise. | Low $\mathcal{O}(N)$ + QRS Detection |
