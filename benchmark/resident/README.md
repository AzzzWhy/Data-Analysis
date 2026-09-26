# Fair resident-engine benchmark

Both pandas and cuDF load the same file once per fresh process. Each runs profile, summary,
groupby, Pearson correlation and IQR outlier counts over every row. Five repeats alternate
CPU/GPU order, warm the file cache, synchronize GPU timing, and compare numerical results.
Raw JSON includes each sample, medians, sample standard deviation, CV, source hashes and versions.

| Input | Load + five operations, CPU / GPU | Including startup, CPU / GPU | Interpretation |
| --- | ---: | ---: | --- |
| 20M rows, 3.04 GB CSV | 2.92x | 2.46x | GPU faster on this workload |
| 1M rows, narrow CSV | 0.379x | 0.277x | CPU faster: 2.64x workflow / 3.61x process wall |

Large-file compute-only median ratio is 1.63x, **not a stable headline**: GPU compute CV >10%.
Small-file CPU initialization CV >10% is also flagged. All retained statistical checks agreed.
The load-stage ratio at 20M rows is 5.07x: parsing materially contributes to the workflow gain.
Avoided rereads are NOT attributed to GPU: neither baseline rereads between operations.

This is NVIDIA GB10, cuDF 25.10 and default pandas 2.3.3, warm-cache CSV, these selected columns
and operations only. It does not establish superiority over Polars, DuckDB or tuned parallel CPU
code, nor a universal file-size threshold. The two files have different widths; do not infer a
size-only crossover from their ratios. These synthetic sales datasets benchmark performance;
the separate UCI demo establishes genuine analytical findings.

Reproduce (RAPIDS Python, CUDA toolkit headers required):

```sh
export CUDA_PATH=/usr/local/cuda
python skills/cudf-analytics/scripts/benchmark_resident.py \
  --input /home/Developer/sales_demo.csv --repeats 5 --out resident-large.json
python skills/cudf-analytics/scripts/benchmark_resident.py \
  --input /home/Developer/sizetest/d_1000000.csv --repeats 5 \
  --columns revenue,cost --agg 'revenue:sum,mean' --out resident-small.json
```

Large sample uses `revenue,cost,quantity`, grouping by region, aggregating revenue sum/mean and
quantity sum. Small sample lacks quantity, so uses revenue/cost and revenue sum/mean. The large
JSON predates the added configurable CLI fields but used exactly the current default parameters;
raw samples were not rewritten. An initial missing-CUDA-header failure was resolved by exporting
CUDA_PATH; its failed log is retained outside the public repository, not counted as a sample.

[Large raw measurements](resident-large.json) · [Small raw measurements](resident-small.json)
