# Genuine-data demo

Source: Georges Hebrail and Alice Berard, [UCI Individual Household Electric Power Consumption](https://archive.ics.uci.edu/dataset/235/individual+household+electric+power+consumption),
DOI 10.24432/C58K54, CC BY 4.0. One household, 2006-12-16 through 2010-11-26.

2,075,259 original minute rows were preserved without sampling, replication or signal injection.
25,979 missing active-power measurements stay null; means exclude them. Added hour, month and
weekday/weekend fields are deterministic timestamp derivations. Source/archive/prepared hashes
and exact cleaning rules are in [provenance.json](provenance.json).

All 24 hourly means and non-null counts were independently recomputed before the skill ran,
then checked against actual skill output. Peak: 20:00, 1.899064 kW. Lowest: 04:00, 0.443847 kW.
Their ratio is 4.27864, not a promised saving or causal explanation. This one household's
historical pattern cannot be generalized to all households.

The 143.5 MB prepared file intentionally runs on CPU pandas. It demonstrates useful real
analysis, not GPU speed; performance is established separately by the resident benchmark.
The first Agent conversation omitted top_k and fabricated a complete table from truncated
top-20 output. The prompt/schema/skill now require enough groups for a complete ranking and
forbid guessing omitted values. The tool adapter now expands unspecified top-K to the complete
group count when it fits its 30-row output budget; larger or explicitly limited results carry
a coverage warning. A deterministic regression checks all 24 hours, the minimum, and top-5.

```sh
python skills/cudf-analytics/scripts/prepare_power_demo.py --download --out-dir .dl/power-demo
```

The official download and full prepared dataset are deliberately excluded from Git.
Retained outputs: [HTML report](report.html), [findings](report.md), [24-hour CSV](groupby.csv),
[actual skill JSON](analysis.json), [Agent conversation](agent-run.txt).
