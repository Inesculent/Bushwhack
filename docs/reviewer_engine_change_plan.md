# Reviewer Engine Change Plan

## Goal and guardrails

The reviewer must remain repository-agnostic. It should reason about what code
does, why that behavior is required, and whether the implementation preserves
the relevant contract. Benchmark examples may measure this behavior, but must
not become prompt patterns, special cases, or issue templates in the pipeline.

Changes follow a subtraction-first rule:

1. remove logic that fabricates confidence, duplicates work, or amplifies weak
   signals;
2. measure the simpler pipeline;
3. add only the smallest missing representation or decision rule justified by
   observed failures.

No new agent or graph stage is planned. New logic must replace weaker logic or
pay for itself by deleting more complexity than it adds.

## Benchmark baseline

The local AACR-Bench data was checked against the public
[Alibaba-Aone dataset](https://huggingface.co/datasets/Alibaba-Aone/aacr-bench/tree/47be1d6df1e7faf222cf531587772d92f79fe6b2)
at revision `47be1d6df1e7faf222cf531587772d92f79fe6b2`, and matching follows the staged
shape in the [official evaluator](https://github.com/alibaba/aacr-bench):

- 2,145 comments: 1,505 positive and 640 negative;
- the six PRs exercised by runs `cbcecf1b9e7b` and `55b282c72b21` contain 50
  positive references and 20 negative comments;
- the two runs emitted 23 findings;
- using the official path/side/line matching shape with `k=1`, 6 findings match
  a positive reference by line (26.1% precision, 12.0% recall);
- manual semantic inspection found two clear matches (8.7% directional
  precision, 4.0% recall). This is diagnostic, not the official LLM semantic
  score, because the official judge was not run.

The baseline says both recall and precision are failing. The pipeline is doing
large amounts of work without reliably carrying contract evidence into the
final finding.

## Change sequence

### Change 1 — Remove fabricated certainty and redundant execution

Cut:

- candidate synthesis when the executor returns `candidate` without a candidate
  payload;
- contract-proof backfilling from the check into an incomplete candidate;
- the unused `suppressed` result state;
- the no-op suppression-audit hook;
- same-batch executor continuation that re-asks terminal checks after another
  check produced a candidate;
- evidence-gate status copies appended to `review_check_results`.

Keep:

- `unsupported` as the explicit state for missing payload or proof;
- evidence-gate lifecycle and reasons in task metadata;
- the original check result as an immutable record of what the executor
  actually returned.

Verify:

- incomplete candidates normalize to `unsupported`, with a diagnostic warning;
- no model-facing check-result field can pre-decide the evidence gate;
- one executor batch produces one canonical result per check and no continuation
  LLM call;
- evidence-gate promotion/drop behavior is unchanged apart from where lifecycle
  status is recorded;
- focused unit tests and graph/schema tests pass.

### Change 2 — Remove weak-signal amplification

Cut generic surface-fill tasks, duplicated scout/continuation passes, and fixed
candidate floors that reward producing more candidates rather than better
evidence. Retain a single coverage diagnostic so missed surfaces remain
observable without forcing speculative findings.

Verify with per-stage counts: planned surfaces, contract questions, checks,
unsupported results, gated candidates, and final findings. Token and wall-clock
cost should fall materially without reducing matched positives.

### Change 3 — Add a minimal contract-evidence representation

Reuse existing contract questions and evidence references. Add only a compact
contract status (`supported`, `missing`, or `contradicted`) plus source
references where the current data model cannot express them. Do not add a new
retrieval or adjudication stage.

The compiler must preserve uncertainty, the focused-context step must retrieve
the missing justification, and the executor may return `no_finding` only when
both implementation evidence and contract evidence support it.

Verify with targeted fixtures for caller, schema, convention, old-behavior, and
framework contracts. Missing justification must remain `unsupported` rather
than becoming a candidate or suppression through fallback text.

### Change 4 — Make benchmark evaluation reproducible

Add an evaluator compatible with the official AACR-Bench stages: path, side,
line (`k=1`), then semantic matching. Pin the dataset revision in run metadata
and keep raw stage metrics separate so architectural regressions are visible.
Do not feed benchmark labels or reference comments into the reviewer.

Verify the evaluator against known synthetic matches and non-matches, then
rerun the six-PR slice before expanding to the full benchmark.

## Acceptance rule

After each change, compare quality, cost, and traceability against the saved
baseline. A change stays only if it removes a demonstrated failure mode or
improves benchmark behavior without making the reviewer benchmark-specific.
