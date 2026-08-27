# Reviewer Engine Resume Handoff

Last updated: 2026-08-25 (Changes 2, 3, and 4 implemented; Change 2 smoke test audited; Change 3 unmeasured)

## Objective

Evaluate and simplify the reviewer pipeline while preserving its original
repository-agnostic purpose: understand what code does, identify why that
behavior is required, and determine whether the implementation preserves the
contract.

Keep two questions separate throughout the work:

1. Does the current logic operate as intended?
2. Is that logic architecturally correct, or should it be changed?

The governing design document is `docs/reviewer_engine_expectation.md`. The
saved implementation plan is `docs/reviewer_engine_change_plan.md`.

## Architectural rule

Work subtraction-first:

1. remove fabricated certainty, duplicated work, and weak-signal amplification;
2. test the simpler pipeline;
3. add only the smallest missing representation or rule justified by observed
   failures.

Do not add benchmark-specific prompts, issue templates, repository-specific
rules, new agents, or new graph stages. Avoid increasing local-LLM context unless
the added evidence replaces less useful context.

## Benchmark verification and baseline

The local data was checked against the actual public
[Alibaba-Aone AACR-Bench dataset](https://huggingface.co/datasets/Alibaba-Aone/aacr-bench/tree/47be1d6df1e7faf222cf531587772d92f79fe6b2)
at revision `47be1d6df1e7faf222cf531587772d92f79fe6b2` and against the staged matching
shape in the [official evaluator](https://github.com/alibaba/aacr-bench).

Dataset baseline:

- 2,145 comments;
- 1,505 positive labels;
- 640 negative labels;
- local `data/raw/aacr_bench_raw.csv` matches those counts.

Runs evaluated:

- `logs/cbcecf1b9e7b`: five PRs, 18 findings, approximately 7.666M tokens and 188
  minutes;
- `logs/55b282c72b21`: ComfyUI PR 7952, five findings, approximately 1.991M
  tokens and 44.7 minutes.

Across the six PRs:

- 50 positive benchmark references;
- 23 emitted findings;
- official-shaped path/side/line matching with `k=1`: 6/23 line precision
  (26.1%) and 6/50 line recall (12.0%);
- manual semantic inspection found two clear matches: 8.7% directional
  precision and 4.0% recall.

The manual semantic result is diagnostic, not the official semantic-judge
score; no official judge credentials were used.

Conclusion: both recall and precision are poor, and the pipeline spends a large
amount of context and execution time without reliably carrying contract
evidence into final findings.

## Completed: Change 1

Change 1 removes fabricated certainty and redundant executor work.

Implemented behavior:

- a `candidate` decision without a candidate payload becomes `unsupported`;
- a candidate missing `expected_behavior`, `evidence_for_contract`,
  `counterexample`, or `rejection_check` becomes `unsupported`;
- the normalizer no longer synthesizes a candidate from reportable text;
- the normalizer no longer fills candidate contract proof from check text,
  generic report criteria, or fallback assertions;
- the unused `suppressed` decision was removed;
- `gate_decision` and `gate_reason` were removed from the model-facing
  `ReviewCheckResult` schema;
- the no-op suppression-audit hook and its dead metrics were removed;
- same-batch executor continuation and its second LLM call were removed;
- the evidence gate now evaluates only the latest canonical result per check;
- the evidence gate records pass/drop lifecycle only in task metadata and no
  longer appends copied results to additive LangGraph state;
- the AACR harness reads gate lifecycle from task metadata.

This is intentionally a net deletion. No replacement agent, stage, retry, or
fallback was added.

Primary implementation files:

- `src/domain/schemas.py`
- `src/orchestration/nodes/application/review_check_executor_support.py`
- `src/orchestration/nodes/application/review_checks.py`
- `src/orchestration/nodes/application/review_adjudicator.py`
- `src/orchestration/prompts/reviewer/review_check_executor.md`
- `src/reviewer_agent/harness/aacr.py`
- `src/orchestration/tests/test_review_checks.py`

## Verification completed

- `src/orchestration/tests/test_review_checks.py`: 150 passed;
- all `src/orchestration/tests`: 644 passed;
- all `src/domain/tests`: 13 passed;
- an additional reviewer-focused group: 144 passed;
- `git diff --check`: clean.

Pytest emitted only a warning that it could not write `.pytest_cache`; test
execution was unaffected.

The executor JSON schema was also checked to ensure it contains none of:

- `gate_decision`;
- `gate_reason`;
- the `suppressed` decision.

## Smoke test completed: run `45b85bd93829` (2026-08-24)

The ComfyUI PR 7952 smoke test was run with the same CLI flags, models
(`qwen3.5-122b` planner and worker), `enforced` check mode, and `remote`
profile as `55b282c72b21`. Provenance: the run's `source_tree_sha256`
(`0170e259...`) equals the hash of the current local `src/` tree, so it executed
Change 1 plus the pre-existing worktree edits. (The `git.commit` recorded in
both runs, `c9ec340`, is a stale remote checkout 30 commits behind local
`master`; only the tree hash identifies what ran.)

Verification rerun on 2026-08-24: `src/orchestration/tests` +
`src/domain/tests` = 657 passed (pass `--basetemp` to a writable directory;
a sandboxed `tmp_path` otherwise produces setup errors, not failures).

### Metrics: `45b85bd93829` vs `55b282c72b21`

| Metric | 45b (post-Change 1) | 55b (baseline) |
| --- | --- | --- |
| Wall-clock | 31.0 min | 44.7 min |
| Total LLM tokens | 1,353,522 | 1,991,060 |
| LLM requests / responses / errors | 112 / 106 / 6 | 178 / 164 / 14 |
| Executor requests (incl. length retries) | 65 (4 retries) | 93 (9 retries) |
| Executor `LengthFinishReasonError` | 4 (20,480 completion tokens each) | 10 |
| Tokens burned in failed calls (total minus successful responses) | ~136K (10%) | ~438K (22%) |
| Plan tasks (planner + surface-fill) | 10 (7 + 3) | 11 (4 + 7) |
| Compiled / valid / invalid checks | 88 / 63 / 25 | 89 / 89 / 0 |
| Invalid reasons | `anchor_not_in_changed_code` x25 | none |
| Executor decisions (latest per check) | candidate 3, no_finding 1, unsupported 41, budget_exhausted 18 | candidate 11, no_finding 4, unsupported 55, budget_exhausted 19 |
| `executor_candidate_missing_payload` | 0 | 0 |
| `executor_candidate_missing_contract_proof` | 0 | n/a (synthesized 0, backfilled 0) |
| Same-batch continuation activations | removed | 0 |
| Gate evaluated / passed / dropped | 3 / 3 (2 + 1 audit-only) / 0 | 11 / 11 (9 + 2) / 0 |
| `review_check_results` rows | 63 (= valid checks, no duplicates) | 100 (89 + 11 gate copies) |
| Candidates / reflections / adjudicator calls | 3 / 2 / 1 | 11 / 9 / 5 |
| Final findings | 1 | 5 |
| Line stage (official one-to-one pairing, k=1) precision, recall | 1/1, 1/5 | 2/5, 2/5 |
| Line stage (hand count allowing multi-matches, superseded) | 1/1, 1/5 | 3/5, 4/5 |
| Manual semantic precision, recall | 1/1, 1/5 | 1/5, 1/5 |
| Schema/parse errors other than length limit | 0 | 0 |

The surviving finding (StringCompare missing return, lines 175-189) is the same
true positive the baseline found. The baseline's other line-stage "matches" were
spurious: a 134-line-span regex-caching finding (190-323) overlapped four
references, and a ReDoS finding (190-227) overlapped the StringCompare
reference by one line.

### What the run shows

1. Change 1 works mechanically: one canonical result per check, no gate copies
   in additive state, gate lifecycle read from task metadata, no `suppressed`
   decision, and zero candidates lost to missing payload or missing contract
   proof. The baseline confirms why the model-facing gate fields had to go:
   the executor LLM stamped `gate_decision="passed"` on 49 `unsupported` and
   16 `budget_exhausted` results, and that text reached the adjudicator packet
   and the coverage audit.
2. Do not credit Change 1 with the cost drop. Every code path it removed had
   zero activations in the baseline run for this PR (continuation 0,
   synthesized candidates 0, proof backfills 0, suppression audits 0,
   `suppressed` 0). The 32% token and 31% wall-clock reductions come from a
   smaller plan, 25 validator-rejected checks, fewer candidates downstream, and
   fewer runaway-output failures (4 vs 10).
3. Validator collapse (new, not caused by Change 1): the planner gave
   `security-01` and `performance-01` `target_files=['nodes.py']` while every
   surface they own lives in `comfy_extras/nodes_string.py`.
   `_anchor_matches_changed_surface` returns False whenever
   `file_path not in task.target_files`, and the validator labels that
   `anchor_not_in_changed_code`. Both tasks lost all 20 checks; 5 more were
   lost in `general-02`, `logic-01`, `logic-02`. 13 of the 25 were
   LLM-compiled, line-specific checks. The baseline avoided this only because
   its plan happened to list both files.
4. Generated fills produce nothing: 43 of 63 valid checks were `coverage:N` /
   `surface:N` / `scout:N` / `contract-question:N` fills and yielded 0
   candidates and 0 `no_finding`; all 3 candidates came from the 20
   LLM-compiled checks. In the baseline, contract-question checks produced 5
   of 11 candidates (four of them duplicates of the one StringCompare issue),
   coverage fills produced 2, and scouts 0. Scout checks are emitted with
   `budget=1` and die immediately (`review_check_no_retry_path`): 4/4 here,
   7/7 in the baseline.
5. The one-line `nodes.py` registration change received 25 of 63 valid checks
   across three tasks, 27 of 65 executor calls, and about 53% of executor
   prompt characters; results were `unsupported` (23), `budget_exhausted` (1),
   and `no_finding` (1).
6. Focused context is mostly unrelated files: of 3.13M chars returned, 71.6%
   came from `comfy/comfy_types/node_typing.py`, `comfy/controlnet.py`,
   `comfy/cldm/cldm.py`, and `comfy/clip_vision.py`; only 5.3% was
   `comfy_extras/nodes_string.py`. The baseline showed the same shape (68%
   unrelated). Executor prompts had a median of 55.7K chars, above the 44K
   multi-check limit, so batches ran as single checks.
7. Runaway output: 4 executor and 2 triage calls hit the completion-token cap
   (20,480 / 12,288) without finishing JSON; each cost 3-5 minutes. Triage
   fell back to declared candidate fields for one candidate.
8. Recall diagnosis for the four missed references: `RegexExtract` (three
   references at lines 275-297) had no line-specific check in this run;
   `logic-03` compiled only seven single-line coverage fills anchored at line
   228, all `unsupported` or `budget_exhausted`. The baseline's surface-fill
   task for `RegexExtract.execute` was the only thing that put a real check on
   those lines. The unused-function reference at line 5 was not addressed by
   any check in either run.
9. Adjudication was consistent: the StringTrim silent-`else` candidate was
   dropped under the declared-input-schema rule; the baseline had emitted the
   identical CaseConverter claim as a finding.

### Decision

The decision rule is satisfied: the run completed cleanly, the known match did
not disappear, and no candidate collapsed into `unsupported` because of missing
payload or proof. Proceed to Change 2, with the amendments below.

### Recommended amendments to the plan

- Change 2 should cut, in order of measured waste: scout checks; per-surface
  lens coverage fills (`coverage:N`, `surface:N`), keeping one uncovered-surface
  diagnostic; the lens matrix for `kind=file` surfaces whose diff is a trivial
  registration line; and focused-context `contract_context_paths` that pull
  whole unrelated files (evidence paths first, then a small cap on non-changed
  files).
- Keep the surface-fill task mechanism until measured: it is what produced the
  only behaviour-shaped check on `RegexExtract` lines in the baseline. Cut the
  fills it compiles, not the task.
- Fix the validator/planner mismatch as a rule correction, not a new stage:
  derive task `target_files` from the surface ledger (or validate a check's
  file against its surface's file) and rename the reason to
  `file_not_in_task_targets`. This touches `planner.py` and `review_checks.py`,
  where uncommitted "Refined target" work already exists; coordinate.
- Record length-limit failures (count, tokens, seconds) in `run_meta`; they were
  22% of baseline tokens and 10% here, and they dominate wall-clock variance.
- For Change 4, report finding span width alongside line-stage matches and do
  not report precision without the semantic stage; wide spans game the line
  stage.
- When comparing future runs, compare per-stage counts (plan tasks, valid
  checks, candidates) rather than totals; plan nondeterminism moved more than
  Change 1 did.

## Completed: Change 2

Change 2 removes weak-signal amplification and corrects two diagnostics that
were lying. It is a net deletion; no stage, agent, retry, or fallback was added.

Removed:

- the `review_check_scout` node and its routing; the evidence gate now ends
  the per-task check subgraph;
- deterministic coverage-floor check generation: obligation fills
  (`coverage:N`), file and surface fallback fills (`surface-coverage:N`,
  file-level `coverage:N`), uncovered-behavior fills
  (`uncovered-behavior:N`), migration and maintainability floor checks, and
  the high-signal swap that kept floor checks inside the cap;
- the LLM coverage-critic second pass over compiler output;
- surface-invariant checks (`surface:N`) derived from the behavioral spec;
  contract-question checks remain the mental model's route into the compiler;
- mining mental-model, repository-KB, and snapshot text for source paths when
  planning focused context.

Kept or corrected:

- `cap_compiled_checks` (formerly `ensure_compiler_coverage_floor`) still
  bounds checks per task with the adaptive cap and owner-fair selection, and
  records the single coverage diagnostic under
  `review_checks.by_task[*].compiler_coverage`: `missed_files`,
  `uncovered_obligations`, `missing_primary_surface_ids`,
  `evidence_omitted_files`, `trimmed_check_ids`, `max_checks`, and
  `compiler_coverage_*` warnings. Nothing is synthesized from it.
- Focused-context contract paths come only from paths the check itself names
  (`required_evidence`, `allowed_retrieval`) or the executor names in its
  latest `missing_evidence`, capped at four; retrieval scope widens only for
  those paths.
- The validator reports `file_not_in_task_targets` or
  `file_not_in_changed_code` before it tests anchors, so
  `anchor_not_in_changed_code` now means exactly that.
- The planner repairs `target_files` from explicit surface ids for every
  specialty: logic tasks are narrowed to their surfaces' files (unchanged);
  other tasks keep their declared targets and gain their surfaces' files.
  Text-inferred surfaces still apply only to logic tasks.
- `run_meta.length_limit_failures` and the manifest columns
  `length_limit_failure_count`, `length_limit_failure_tokens`, and
  `length_limit_failure_ms` record completion-cap failures per run and per PR.

Primary implementation files:

- `src/orchestration/nodes/application/review_check_compiler_support.py`
- `src/orchestration/nodes/application/review_checks.py`
- `src/orchestration/nodes/application/critique_pipeline.py`
- `src/orchestration/nodes/application/planner.py`
- `src/reviewer_agent/harness/aacr.py`
- `src/orchestration/tests/test_review_checks.py`
- `src/orchestration/tests/test_reviewer_nodes.py`

Verification (2026-08-24): `src/orchestration/tests` + `src/domain/tests` =
644 passed; `src/infrastructure/tests` + `src/reviewer_agent` = 140 passed,
9 skipped, and one order-dependent failure
(`test_run_profile.py::test_apply_run_profile_remote_sets_apptainer_backend`)
that passes when run alone and touches no changed code.

Expected effect on PR 7952, projected from the `45b85bd93829` trace and not
yet measured: 38 of 63 valid checks were fills or scouts (0 candidates); 3 of
13 compiler LLM calls were the critic; 72% of focused-context characters came
from KB-mined files; 25 checks were rejected by the target-file mismatch. The
true-positive candidates all came from LLM-compiled or contract-question
checks, which are untouched.

No benchmark run has been performed after Change 2. Do not claim any effect
until the ComfyUI PR 7952 smoke test is rerun.

### Next smoke test: what to compare

Rerun ComfyUI PR 7952 with the `45b85bd93829` configuration and compare, per
stage, against both `55b282c72b21` and `45b85bd93829`:

- plan tasks and `task_target_files_repaired_from_surfaces` rows;
- compiled / valid / invalid checks and the validator reason counts
  (`file_not_in_task_targets` should replace the old anchor label);
- valid checks by `compiled_check_origins.origin_kind` (expect only
  `llm_compiled`, `contract_question`, `deterministic_fallback`);
- `compiler_coverage.missed_files`, `uncovered_obligations`, and
  `missing_primary_surface_ids` per task;
- compiler LLM calls per task (expect exactly one);
- executor calls, tokens, and prompt sizes;
- focused-context characters by file (expect the changed files to dominate);
- executor decisions, gate promotions, candidates, findings;
- `evaluation.json`: official line-stage numbers, `lost_at` per reference,
  negative line matches, span widths (semantic only if a judge is configured);
- `length_limit_failures` in `run_meta`.

Decision rule: proceed to Change 3 if the StringCompare match survives, no
task loses all of its checks, and executor calls fall without the
`RegexExtract` surface losing its only LLM-compiled coverage. If a surface
that previously had a candidate now has none, inspect `compiler_coverage`
before adding anything back.

## Smoke test after Change 2: run `1a8b4d61dc78` (2026-08-24)

Same PR (ComfyUI 7952), CLI flags, models, and profile as `45b85bd93829`.
Provenance: tree `05655140...`, which is not the Change 1 tree (`0170e259...`)
and predates Change 4 (the run produced no `evaluation.json` or `dataset_pin`).
Change 2 signatures are all present: `compiler_coverage` diagnostics, exactly
one compiler LLM call per task, no scout node, zero invalid checks. It was
scored retroactively with the Change 4 evaluator (`evaluation.json` and
`official/` now exist in the run directory).

### Metrics: `1a8b4d61dc78` vs `45b85bd93829` vs `55b282c72b21`

| Metric | 1a8b (Change 2) | 45b (Change 1) | 55b (baseline) |
| --- | --- | --- | --- |
| Wall-clock | 57.0 min | 31.0 min | 44.7 min |
| `total_llm_tokens` (run_meta) | 940,248 | 1,353,522 | 1,991,060 |
| Tokens in successful responses | 812,098 | 1,217,381 | 1,552,972 |
| Length-limit failures: count / server-reported tokens / model time | 18 / 475,656 / 73.5 min | 6 / 166,634 / 21.4 min | 14 / 401,569 / 59.2 min |
| LLM requests | 139 (13% failed on length) | 112 (5%) | 178 (8%) |
| Plan tasks (planner + surface-fill) | 12 (7 + 5) | 10 (7 + 3) | 11 (4 + 7) |
| Compiled / valid / invalid checks | 48 / 48 / 0 | 88 / 63 / 25 | 89 / 89 / 0 |
| Valid checks by origin | llm 25, contract_question 15, fallback 8 | llm 20, cq 5, coverage 31, scout 4, invariant 3 | llm 23, cq 24, coverage 31, scout 7, critic 3, surface 1 |
| Compiler LLM calls | 12 (one per task) | 13 (10 + 3 critic) | 14 (11 + 3 critic) |
| Executor calls / length errors / successful tokens | 52 / 13 / 362K | 65 / 4 / 889K | 93 / 10 / 1,028K |
| Executor prompt chars, median | 32.1K | 55.7K | 47.2K |
| Focused-context chars (share from the two changed files) | 450K (52%) | 3.13M (11%) | 3.30M (19%) |
| Executor decisions (cand / no_finding / unsupported / budget) | 10 / 3 / 26 / 9 | 3 / 1 / 41 / 18 | 11 / 4 / 55 / 19 |
| Gate passed / dropped | 10 / 0 | 3 / 0 | 11 / 0 |
| Candidates -> findings | 10 -> 8 | 3 -> 1 | 11 -> 5 |
| Official line stage (one-to-one, k=1) P / R / F1 | 2/8, 2/5, 0.308 | 1/1, 1/5, 0.333 | 2/5, 2/5, 0.400 |
| Manual semantic true positives | 2 (StringCompare return; `group_index` off-by-one) | 1 | 1 |
| `lost_at` per reference | no_check 1 (L5), executor:no_finding 1 (L275-283), matched 2, final_line_mismatch 1 | no_check 3, executor:unsupported 1, matched 1 | matched 2, executor:no_finding 1, final_line_mismatch 2 |
| Negative (label 0) line matches | 0 | 0 | 0 |

### What the run shows

1. The cuts behaved as predicted. No coverage fills, scouts, invariants, or
   critic passes; focused context shrank 86% and is now dominated by the
   changed files; executor prompts are a third smaller; the validator
   rejected nothing because the planner repair fixed six logic tasks whose
   targets were hallucinated paths (`nodes/string_concatenate.py`, ...).
2. Recall improved for the right reason. `RegexExtract.execute` received
   seven line-specific checks (three LLM-compiled, four contract questions)
   and produced the `group_index` true positive. In 45b that surface had no
   check at all. Every reference now has a task, and four of five have an
   overlapping check.
3. Precision fell: eight findings, two true positives. The other six are the
   StringLength grapheme claim, the Unicode case-folding claim (also emitted
   in 55b), two StringConcatenate type/length claims, an empty-pattern claim,
   and the `re.error` conflation claim. Three of those went
   `needs_verification` -> verifier `inconclusive` -> promoted anyway; that
   verifier fallback path (pre-existing worktree work) deserves a look.
4. The remaining generic path fired: `fallback_checks` produced eight
   obligation-shaped checks in `general-1` and `logic-5` (the latter after
   the compiler call itself hit the length cap); seven ended
   `budget_exhausted`, none produced a candidate. Same profile as the
   removed floors.
5. Cost moved in two directions. Productive tokens fell 33% against 45b and
   48% against 55b, but wall-clock rose to 57 minutes because 18 calls hit
   the completion cap (13 executor, 4 triage, 1 compiler): 476K tokens and
   73 minutes of model time that produced nothing, and 3 of the 10 compact
   retries failed the same way. This is nondeterministic model behaviour,
   not pipeline structure, and it now dominates every timing comparison.

### Decision

The Change 2 decision rule is satisfied: the StringCompare match survived, no
task lost all its checks, executor calls fell, and `RegexExtract` gained
real coverage. Proceed to Change 3, but bound the runaway-output failures
first (a lower completion cap or compact-first retry for the executor and
triage, measured the same way), because they corrupt every cost comparison.
Also note the validity issue below: none of the three runs reviewed the
annotated commit, so their line-stage numbers are against the wrong version
of the code.

## Benchmark validity: the reviewed commit was not the annotated commit

While attributing the `L5` reference ("unused `normalize_path`") the
evaluator showed the reviewer's ledger had `class StringConcatenate` at line
5. The cause: the harness fetched the *live* PR diff (`pull/N/head`), while
the references were annotated on the dataset's pinned commits. For PR 7952
the live head is `7761207` (force-pushed on 2025-05-09 after review; the
`normalize_path` helper was removed), and the annotated head `4936d01` no
longer exists on GitHub (422) or in the contributor's fork. The official
evaluator clones the repo and checks out `head_commit` before reviewing, so
its reviewers see the annotated version.

Across the 22 PRs in the local processed CSV: the annotated head differs from
the live head for all 22; it is still resolvable on GitHub for 19; it is gone
for `comfyanonymous/ComfyUI#7952` and `#9560`, and `vllm-project/vllm#19231`
has no positive record at all. Every run so far (the six-PR baseline and the
three 7952 smoke tests) reviewed the live version, so their line matches are
partly luck: 7952's matches survived only because the affected code moved by
a few lines.

Fix (in this worktree, harness-side only):

- `GitHubPullRequestEnricher.fetch_compare_diff` fetches the diff for the
  pinned `source_commit...target_commit` range via the compare API;
- the harness reviews that range and passes `review_checkout_ref =
  target_commit` into state, which the review context already honours when
  it clones into the sandbox (it previously fell back to `pull/N/head`);
- when the annotated commit is unresolvable the run reviews the live head,
  adds the run warning `annotated_head_unresolvable`, and records
  `reviewed_code_version = live_head`; manifests, `evaluation.json`,
  `run_meta.evaluation_summary.code_version_counts`, and the official export
  all carry the version, and runs recorded before this change are labelled
  `live_head` when re-scored.

Consequences for the plan:

- PR 7952 cannot be scored line-accurately by anyone, including the official
  pipeline; it should stop being the smoke-test target. Pick a Python PR
  whose annotated head is resolvable (19 candidates above; the ComfyUI ones
  `#8446`, `#6033`, `#6542` keep the repository fixed).
- The first run on the annotated commit is a new baseline; compare per stage
  with the earlier runs but do not compare line-stage numbers across the two
  code versions.
- `docs_prebrief` still reads repository docs at the PR's base branch; only
  the changed-file reads and the diff are pinned.

## Completed: Change 4

Every AACR run now scores itself against the official references and emits
the official evaluator's input layout, so the comparison table above no longer
has to be assembled by hand. Nothing here touches the reviewer graph: scoring
runs in the harness after `final_findings` are written, and references are
never placed in state or prompts.

Implementation: `src/reviewer_agent/harness/aacr_eval.py` (new) and
`src/reviewer_agent/harness/aacr.py`; tests in
`src/orchestration/tests/test_aacr_eval.py`.

### What is vendored, and from where

The official evaluator (https://github.com/alibaba/aacr-bench, commit
`68a569759289a83654a59d06db2a72910edf0a4a`, 2026-08-24) is transcribed rather
than imported because its modules are top-level names (`config`, `schema`,
`judge`) that collide with this repository:

- `evaluation/judge.py`: path normalization, `diff_location_is_same`
  (overlap or minimum distance <= k), the reference-ordered one-to-one pairing
  in `evaluate_comments` (each generated comment can supply one line match and
  one semantic match), `compute_cr_statistics`, the semantic-judge prompt and
  answer parsing;
- `evaluation/evaluate.py`: line-range normalization, the OCR result shape
  (`review.comments[] {path, start_line, end_line, content}`), summary/F1;
- `evaluation/converters/aacr_bench.py`: the `ReviewInstance` mapping and the
  `instance_id` rule `owner__name@<target_commit[:7]>` (their converter maps
  `source_commit` -> base and `target_commit` -> head, the reverse of the
  local processed CSV; the export takes the commits from the official file).

Validation: both existing runs were exported and scored with the upstream
`evaluate.py` itself (mock judge, so only line-stage numbers are meaningful);
per-reference `line_match` flags and all line-stage summary numbers were
identical to the in-repo scorer, and the upstream code read the exported
duration and token usage.

### Run artifacts added

- `evaluation.json` (run level) and `evaluation/<slug>.json` (per PR): the
  official statistics (`line_match_rate`, `line_recall_rate`, `line_f1`,
  semantic equivalents when a judge ran), per-reference `line_match` /
  `semantic_match`, per-finding `span_width` and which reference consumed it,
  and two local diagnostics kept apart from official numbers:
  `attribution[].lost_at` per reference (`no_task`, `no_check`,
  `check_invalid:<reason>`, `executor:<decision>`, `gate_dropped:<reason>`,
  `adjudicator_<decision>`, `final_consumed`, `final_line_mismatch`,
  `matched`) and `negative_line_matches` (findings within k lines of a
  label-0 comment from the annotated dataset).
- `official/results/<safe_id>.json`, `official/aacr_bench_slice.jsonl`, and
  `official/README.md` with the exact `python -m pipeline run --stage eval
  --reviewer ocr ...` command for the canonical score with their judge.
- Manifest columns: `line_match_count`, `line_match_rate`,
  `line_recall_rate`, `semantic_match_count`, `semantic_status`,
  `negative_line_match_count`, `reference_lost_at`, `evaluation_path`.
- `run_meta.json`: `dataset_pin` (path, sha256, PR/comment counts, upstream
  meta sha256 and whether it matches, upstream commit, HF revision),
  `evaluation_summary`, `official_export`, and the run warnings
  `positive_samples_missing` / `positive_samples_sha256_differs_from_upstream_meta`.
- Existing runs can be scored retroactively:
  `python -m src.reviewer_agent.harness.aacr_eval logs/<run_id>`.

### Reference data

The harness reads `documentation/dataset/positive_samples.json` (gitignored)
and now fetches it from the upstream URL when absent instead of silently
scoring against nothing, which is what this checkout had been doing. The
fetched file hashes to `7a4a0e70...` (196 PRs, 1,506 comments) while the
upstream `positive_samples.meta.json` still pins `d8683cb2...`; upstream's
own data has drifted from its pin, so every run records the actual sha256.
The Hugging Face revision `47be1d6d...` (2,145 labelled comments) remains
the source of negatives via `data/raw/aacr_bench_raw.csv`.

The semantic stage runs in-run only when the official `JUDGE_BASE_URL`,
`JUDGE_API_KEY`, and `JUDGE_MODEL` variables are set; mock similarity is never
reported as a score. Without a judge, `semantic_status` is `not_run`.

### Corrected baseline numbers

Under the official one-to-one pairing the earlier hand-computed line-stage
numbers for the baseline were too generous: `55b282c72b21` is line precision
2/5 and recall 2/5 (the 134-line finding can be consumed by only one
reference), not 3/5 and 4/5. `45b85bd93829` stays at 1/1 and 1/5. The
attribution for 45b is `matched` (L189-194), `executor:unsupported` (L5), and
`no_check` for the three RegexExtract references, which is what the manual
audit found.

## Completed: Change 3

Change 3 adds the minimal contract-evidence representation and deletes the
text heuristics it replaces. No stage, agent, or retrieval path was added.

### What the `1a8b4d61dc78` trace justified

- 27 of 48 executor results were `no_finding` answers downgraded by
  `missing_owned_scope_variant`, a test that the suppression text literally
  contained the check's `owned_contract_scope` string; two more fell to the
  mode-phrase regex (`cross_variant_displacement`). Every inspected case was
  a wrong downgrade (the CaseConverter and RegexExtract mode-completeness
  suppressions cited the declared enum). Each downgrade replaced
  `missing_evidence` with the check's own `required_evidence[:3]` (28 of 35
  unsupported/budget results), so focused context re-fetched code already in
  the packet and the checks ended `budget_exhausted`. That is where most of
  the executor calls went.
- The L275-283 reference was lost at `executor:no_finding`. The check's
  `expected_behavior` was implementation mechanics ("First Group checks
  len(match.groups()) >= group_index ..."), the normalizer marked it
  `audit_only`, which exempted it from contract justification, and the
  executor suppressed with "source code shows explicit bounds checking":
  implementation evidence only, no contract source.
- Six of eight final findings were false positives whose contract evidence
  was absent or inferred ("no documentation says otherwise", "based on user
  expectation from the node name"); `logic-1:003` was emitted as a candidate
  while its own `missing_evidence` named the framework documentation it
  lacked.
- `_suppression_has_contract_justification` accepted "documented Python
  behavior" as a contract source because it contained the word "documented".

### Representation added (`src/domain/schemas.py`)

- `ContractSourceRef {kind, ref, note}` with kinds `pr_intent`,
  `old_behavior`, `schema`, `caller`, `framework`, `convention`, `doc`,
  `test`, `representation`.
- `ContractQuestion.contract_source_kind`: the mental model states the kind
  of evidence behind `contract_evidence`; unset when the contract is inferred.
- `ReviewCheck.contract_source`: the compiler names the source or leaves it
  unset. Unset preserves uncertainty: `required_evidence` must then say what
  would establish the contract, and `normalize_compiled_checks` allows
  `focused_context` retrieval for exactly those checks (this replaces the
  marker-word rule that appended the "contract-justification evidence: ..."
  boilerplate).
- `ReviewCheckResult.contract_status` (`supported | missing | contradicted`,
  default `missing`), `contract_source`, and `missing_contract_source`.

### Rules (`review_check_executor_support.py`, deterministic)

- `no_finding` stands only with `contract_status = supported` and a
  referenced `contract_source`, on top of the existing implementation-evidence
  requirement (`evidence_refs` on the check file or focused context, non-empty
  suppressing evidence). Otherwise it becomes `unsupported` with
  `exact_question_mismatch:contract_missing`, `contract_contradicted`, or
  `contract_source_unreferenced`. Audit-only checks are no longer exempt.
- `candidate` stands only with `contract_status = contradicted` and a
  referenced source; otherwise `unsupported` with
  `candidate_contract_unbacked:<reason>`.
- Retrieval targets for a downgraded answer are only what the executor named:
  `missing_contract_source` first, then its `missing_evidence`. The check's
  own `required_evidence` is used only for checks the executor never answered
  (omitted result, batch failure, length-retry failure). An `unsupported`
  answer with nothing named is terminal instead of looping.
- The schema-enum exemption for declared-mode/fallback checks now requires a
  supported `schema` source instead of marker phrases.

Removed: `check_requires_contract_justification`,
`CONTRACT_JUSTIFICATION_REQUIREMENT`, `_suppression_has_contract_justification`,
`_suppression_omits_scope_variant`, `_suppression_displaces_owned_variant` and
its mode-phrase regex, `_suppression_basis_is_operation_only`,
`_schema_enforcement_is_exact_mode_suppression`, the dead
`executor_source_only_override_check_ids` metadata, and the executor-prompt
instruction to echo `owned_contract_scope`.

Kept: the `answer_scope` self-declaration (`neighboring_answer_scope`), the
empty/generic-basis check, focused-context degradation, and the value-flow
requirement for cardinality/serialization checks
(`missing_exact_transformation_scope`); none of these was shown to be harmful
in the trace.

Plumbing: the executor packet carries the compiler's `contract_source`;
adjudicator and triage packets carry `contract_status`, `contract_source`,
and `missing_contract_source`; executor task metadata adds
`executor_contract_status_counts`, `executor_candidate_contract_unbacked`,
and `executor_candidate_contract_unbacked_count`; `evaluation.json`
attribution rows add `executor_contract_status` per overlapping check (runs
recorded before this change show `""`).

Prompts: `review_check_executor.md` (two-layer decision, status semantics,
what is not a contract source), `review_check_compiler.md` (name
`contract_source` or leave it unset and say what would establish it; "Do not
invent a source"), `mental_model/owner_contract_questions.md`
(`contract_source_kind`).

Primary implementation files:

- `src/domain/schemas.py`
- `src/orchestration/nodes/application/review_check_executor_support.py`
- `src/orchestration/nodes/application/review_check_source_scope.py`
- `src/orchestration/nodes/application/review_check_compiler_support.py`
- `src/orchestration/nodes/application/review_checks.py`
- `src/orchestration/nodes/application/review_adjudicator.py`
- `src/reviewer_agent/harness/aacr_eval.py`
- `src/orchestration/tests/test_review_checks.py`, `test_aacr_eval.py`

Verification (2026-08-25): `src/orchestration/tests` + `src/domain/tests` =
682 passed, including the new fixtures: `no_finding` with each source kind
(caller, schema, convention, old_behavior, framework) stands; missing,
contradicted, and unreferenced sources downgrade; unstated contract gets no
fallback retrieval text; candidates with a missing or supported contract or
an unreferenced source become `unsupported`; `missing_contract_source` is
the first retrieval target and its path reaches the focused-context request;
compiler mapping from `contract_source_kind`; packet, metadata, JSON-schema,
and prompt checks. `src/infrastructure/tests` + `src/reviewer_agent`: 140
passed, 6 skipped, the known order-dependent `test_run_profile` failure, and
three `test_sandbox.py` errors (the Docker image `agent-fs-sandbox:latest` is not built on this machine, so they error instead of skipping; the `test_run_profile` failure predates Change 3 and touches no changed code). `git diff --check` clean.

### Expected effect and risk (unmeasured)

Fewer, contract-backed candidates: the six 1a8b false positives would have
been `unsupported`, with the source they said was missing queued for
retrieval. `no_finding` answers no longer fall to text identity, retries
happen only when the executor names a source to fetch, and
implementation-shaped checks cannot be suppressed on mechanics alone. The
risk is mechanical stamping: the executor may write `contradicted` or
`supported` the way it once wrote `gate_decision = passed`. The next smoke
test must read `contract_source.ref` values against the packet, not just the
status counts.

### Next smoke test: what to compare (Change 3)

Per stage against `1a8b4d61dc78` (a different PR is unavoidable; see the
validity section), read:

- `executor_contract_status_counts` and, per result, whether
  `contract_source.ref` points at something in the packet;
- downgrade reasons (`contract_missing` and friends replacing
  `missing_owned_scope_variant`) and how many `unsupported` results are
  terminal (empty `missing_evidence`) versus retried;
- `executor_candidate_contract_unbacked` events and whether the six 1a8b
  false-positive families (grapheme length, Unicode case folding, type
  coercion, length limits, empty pattern, `re.error` conflation) reappear;
- `lost_at` and `executor_contract_status` per reference;
- executor calls, tokens, and length failures (still unbounded).

Decision rule: keep Change 3 if surviving candidates carry a referenced
source, true-positive families still reach the gate, and executor calls per
check do not rise. If recall falls because the executor labels real contract
sources `missing`, inspect the `missing_contract_source` text before
loosening any rule.

## Remaining plan

### Change 2 — Remove weak-signal amplification

Done; see "Completed: Change 2". Its benchmark effect is unmeasured until
the PR 7952 smoke test is rerun.

### Change 3 — Minimal contract-evidence representation

Done; see "Completed: Change 3". Its benchmark effect is unmeasured until a
smoke test runs on a PR whose annotated head is resolvable.

### Still open

- Bound the runaway-output failures (completion cap or compact-first retry
  for executor and triage); 18 calls burned 476K tokens in `1a8b4d61dc78`.
- Decide `fallback_checks`: eight deterministic obligation checks in
  `1a8b4d61dc78` produced no candidate (seven `budget_exhausted`), the same
  profile as the floors removed in Change 2, and their `required_evidence`
  carried unrelated KB facts (`.ci/update_windows/update.py`). Under Change 3
  they compile with no `contract_source`. Remove them once a run confirms the
  profile, or keep them only as coverage diagnostics.

### Change 4 — Reproducible benchmark evaluator

Done; see "Completed: Change 4". Runs now self-score and export the
official evaluator's input layout.

## Worktree safety

The worktree already contained user changes before Change 1. Do not overwrite,
revert, or attribute all current modifications to this reviewer task.

Pre-existing changes were observed in files including:

- `documentation/VERIFIER_SUBGRAPH.md`;
- `src/orchestration/nodes/application/planner.py`;
- parts of `src/orchestration/nodes/application/review_adjudicator.py`;
- earlier sections of `src/orchestration/nodes/application/review_checks.py`;
- `src/orchestration/nodes/verifier/sandbox_executor.py`;
- `src/orchestration/tests/test_review_adjudicator.py`;
- earlier sections of `src/orchestration/tests/test_review_checks.py`;
- `src/orchestration/tests/test_verifier_lint_runs.py`;
- deleted artifacts under `da3ea2b38854/`.

The reviewer changes were made surgically around those edits. Inspect the diff
by file and preserve unrelated work.

## Resume checklist

1. Read `docs/reviewer_engine_expectation.md`.
2. Read `docs/reviewer_engine_change_plan.md`.
3. Read this handoff completely, including the `1a8b4d61dc78` results and the
   "Benchmark validity" section.
4. Run `git status --short` and inspect existing unrelated changes.
5. Bound the runaway-output failures (completion cap or compact-first retry
   for executor and triage) and verify with unit tests; they are the dominant
   cost and corrupt timing comparisons.
6. Pick a smoke-test PR whose annotated head is resolvable (not 7952) and run
   it on the current tree (Changes 1-4); the run scores itself and records
   `reviewed_code_version`, which must be `annotated_head`.
7. Compare per stage with `1a8b4d61dc78` using `evaluation.json` and the
   "Next smoke test: what to compare (Change 3)" list; read
   `contract_source.ref` values against the packets, not just the status
   counts. Treat the new run as the first valid line-stage baseline.
8. Decide `fallback_checks` from that run (see "Still open").

## Document tracking status

At the time this handoff was created:

- `docs/reviewer_engine_change_plan.md` was already staged as a new file;
- `docs/reviewer_engine_resume_handoff.md` was new and untracked.

The handoff will remain untracked unless explicitly added to Git. Its creation
did not change the existing staging state of the plan.
