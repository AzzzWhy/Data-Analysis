# cuDF vs pandas — measured on the host above

- Baseline: pandas 2.3.3 (default single-threaded ops), 20 logical cores
- Accelerated: cuDF 25.10.00 on NVIDIA GB10
- Median of 3 repeats; device synchronized before each timed region
- Platform: Linux-6.14.0-1015-nvidia-aarch64-with-glibc2.39 (aarch64)

| Rows | File (MB) | Step | pandas (s) | cuDF (s) | Speedup |
| ---: | ---: | :--- | ---: | ---: | ---: |
| 3,000,000 | 563.9 | read | 1.673 | 0.202 | **8.30x** |
| 3,000,000 | 563.9 | mean | 0.003 | 0.001 | **2.45x** |
| 3,000,000 | 563.9 | groupby | 0.056 | 0.007 | **8.25x** |
| 3,000,000 | 563.9 | corr | 0.076 | 0.040 | **1.89x** |
| 3,000,000 | 563.9 | quantile | 0.037 | 0.019 | **1.90x** |
| 3,000,000 | 563.9 | total | 1.845 | 0.269 | **6.86x** |
| 10,000,000 | 1882.4 | read | 5.843 | 0.704 | **8.30x** |
| 10,000,000 | 1882.4 | mean | 0.012 | 0.002 | **5.90x** |
| 10,000,000 | 1882.4 | groupby | 0.217 | 0.018 | **11.89x** |
| 10,000,000 | 1882.4 | corr | 0.262 | 0.119 | **2.20x** |
| 10,000,000 | 1882.4 | quantile | 0.127 | 0.059 | **2.13x** |
| 10,000,000 | 1882.4 | total | 6.459 | 0.902 | **7.16x** |
| 30,000,000 | 5669.5 | read | 17.464 | 2.420 | **7.22x** |
| 30,000,000 | 5669.5 | mean | 0.027 | 0.004 | **6.04x** |
| 30,000,000 | 5669.5 | groupby | 0.627 | 0.050 | **12.44x** |
| 30,000,000 | 5669.5 | corr | 0.764 | 0.336 | **2.27x** |
| 30,000,000 | 5669.5 | quantile | 0.405 | 0.175 | **2.31x** |
| 30,000,000 | 5669.5 | total | 19.286 | 2.988 | **6.45x** |

`total` is the end-to-end pipeline (read + mean + groupby + corr + quantile).
Accuracy deltas between the two engines are recorded in `benchmark_results.json`.
