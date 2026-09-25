# Innovation options, evaluated

Goal: add to the existing code rather than rebuild it, and produce something a reviewer can
see the point of.

Each candidate is scored on four things:

- GPU necessity: does this need a GPU at all? If a CPU can do it, the innovation claim is weak,
  and the attribution experiment in this repository already showed how weak.
- Demo value: can it be explained in one sentence and understood on sight?
- Cost: incremental work, estimated against the code that already exists.
- Risk: what could go wrong on stage.

## Candidate A: outlier drill-down

After finding outliers, answer where they are concentrated. Cross the dimensions, compute each
combination's share of outliers, and report the combinations that are significantly
over-represented.

Example: `revenue` has 10.37% outliers; drilling down shows that 31% of the outliers sit in
`region=APAC & category=gamma`, rather than being spread evenly.

| | |
| :--- | :--- |
| GPU necessity | High. The number of dimension combinations multiplies: 5 regions by 4 categories by however many other dimensions, each scanned over the full dataset. Two or three dimensions crossed on a CPU is already slow; on a GPU it is direct. This load suits a GPU naturally. |
| Demo value | High. "Where are the outliers concentrated" is the question the business actually has, and it is far more useful than "how many outliers are there". |
| Cost | Medium. Reuses the existing group-by kernel plus one new drill-down operation. |
| Risk | Low. The algorithm is straightforward and the result can be checked by hand. |

## Candidate B: data quality audit

Turn the tool from an analyzer into a checkup. One scan reports missing-value patterns (which
columns are missing together, suggesting a shared cause), duplicate rows, inconsistent types,
constant columns, likely primary keys, and outliers, as a severity-ranked problem list.

| | |
| :--- | :--- |
| GPU necessity | Medium. Mostly full scans with grouped counts, where a GPU helps but is not required. |
| Demo value | Medium to high. This is the most practically useful thing in real data work, and it lands with an audience easily. |
| Cost | Medium. Several independent checks assembled into one report. |
| Risk | Low, but a feature pile reads as ordinary engineering rather than insight. |

## Candidate C: out-of-core analysis at scale

Frame the comparison as "pandas cannot do this at all" rather than "this is faster", by picking a
size at which pandas fails outright.

| | |
| :--- | :--- |
| GPU necessity | High. This is the architectural advantage showing directly: unified memory and direct GPU access. |
| Demo value | High. "pandas died, cuDF is still running" lands harder than any speedup ratio. |
| Cost | Known. `memory_ceiling_test.py` already does this, but it only reached 60M rows, where pandas had not failed yet. |
| Risk | High. It requires finding a size where pandas genuinely fails (a 120M-row file is around 18 GB), and GB10 has 121 GB of memory, so the failure point may be very far out. That means generating 50 GB or more of data, which is slow and fills the disk. |

## Candidate D: uncertainty quantification

Report confidence intervals alongside point estimates: bootstrap resampling over several
thousand draws for means and quantiles, or a significance test for correlations, with an
explicit warning when the sample is too small for the conclusion to be statistically meaningful.

| | |
| :--- | :--- |
| GPU necessity | High. Bootstrap is naturally parallel: B resamples, each a full aggregation. On a CPU that is B times the work; on a GPU the resamples run in parallel and it is nearly free. Few workloads are genuinely out of reach for a CPU, and this is one. |
| Demo value | High, and it addresses a real complaint: nobody knows whether a number an LLM produced is trustworthy. |
| Cost | High. Needs a resampling kernel and interval computation, plus numeric correctness, where this repository's habit of independent verification helps. |
| Risk | Medium. Bootstrap over very large data needs care: full resampling is memory-hungry, so it needs a chunked or subsampled strategy, and the strategy has to be stated honestly. |

## Candidate E: natural-language orchestration of an analysis plan

Have the agent decompose a complex business question into several skill calls: profile for column
names, filter, group, drill into outliers, summarize.

| | |
| :--- | :--- |
| GPU necessity | None. This is an agent capability, not a GPU capability. |
| Demo value | High, but this is autonomy rather than innovation. It should be built, and it serves judging criterion 1 rather than criterion 3. |
| Cost | Low. The existing loop already supports multiple rounds; it needs a complex task and prompt work. |
| Risk | Low, but it does not answer criterion 3. |

## Recommendation: A, then D

A (outlier drill-down) has the best return. The GPU need is real, the business value is
immediate, the cost is controllable, and the risk is low.

D (uncertainty quantification) tells the best story, because it gives the tool a new position:
not "we compute fast" but "we compute fast and the result is checkable". It is also one of the
few loads a CPU genuinely cannot carry, and the attribution methodology already in this
repository is what proves it.

Together they support one narrative:

> A data analysis agent that reports statistics, tells you how uncertain they are, and attributes
> outliers to specific dimensions. On a CPU those last two are either tens of times slower or
> impossible.

That narrative covers criterion 3 and the GPU-necessity question, which is the weakest point of
this project, since the attribution experiment already showed that most of the end-to-end gain
comes from parallel I/O. Loads a CPU cannot carry are what close that gap.

C is not recommended. Generating tens of GB of data and betting that pandas fails at a workable
scale is a bad trade given the time available.

E should be built but does not count as innovation. It improves autonomy, costs little, and is
worth doing alongside the rest: design one or two multi-step tasks and add them to the demo.

## Open questions

1. Proceed in the order A first, then reassess D?
2. Is there real business data this could run on? Outlier attribution demos far better on
   dimensions that mean something (region, category, channel) than on synthetic data.
3. If D is built, subsampling or full chunked resampling? Full chunked resampling is the better
   answer, provided the strategy and the sample count are stated in the output.