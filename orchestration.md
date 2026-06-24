# Reviewer Orchestration

This document describes the current reviewer graph as implemented in `src/orchestration/reviewer_graph.py`, including how preflight feeds the graph and which parts are still incomplete.

## High-Level Flow

The reviewer graph has one current implementation path. It builds or loads
structural/semantic repository context, synthesizes a `BehavioralSpec`, plans
review tasks against that spec, then runs the adversarial check-first review path
by default.

```mermaid
flowchart TD
    start([START]) --> docsPrebrief{Docs pre-brief enabled and not done?}
    docsPrebrief -->|yes| docsPrebriefNode[docs_prebrief]
    docsPrebrief -->|no| routeInitial{Initial context route}
    docsPrebriefNode --> routeInitial
    routeInitial -->|local repo_path| structuralExtractor[structural_extractor]
    routeInitial -->|remote repo URL| sandboxStructuralExtractor[sandbox_structural_extractor]
    routeInitial -->|preflight + structural graph already present| intentExtractor[intent_extractor]

    structuralExtractor --> semanticMerge[semantic_merge]
    sandboxStructuralExtractor --> semanticMerge
    semanticMerge --> intentExtractor
    intentExtractor --> reviewHistoryContext[review_history_context]
    reviewHistoryContext -->|history enabled / enough context| mandateExplorer[mandate_explorer]
    reviewHistoryContext -->|skip bootstrap| mandatePatch[mandate_patch]
    mandateExplorer --> mandatePatch[mandate_patch]
    mandatePatch --> draftPlanner[draft_planner]
    mandatePatch -->|plan revision requested| planRevision
    draftPlanner --> planCritic[plan_critic]
    planCritic -->|aligned| mandateFinalize[mandate_finalize]
    planCritic -->|needs more mandate evidence| targetedExplorer[mandate_explorer_targeted]
    targetedExplorer --> mandatePatch
    planCritic -->|revise plan| planRevision[plan_revision]
    planRevision --> planCritic
    mandateFinalize --> planEmit[plan_emit]
    planEmit --> snapshotPin[snapshot_pin]

    snapshotPin --> routeCritiqueTasks{Fan out planned tasks}
    routeCritiqueTasks -->|Send per task| critiqueProbe[critique_context_probe]
    critiqueProbe --> mentalEnricher[mental_model_context_enricher]
    mentalEnricher -->|reviewer_check_mode=off| generalCritiquer[general_critiquer]
    mentalEnricher -->|log_only or enforced| reviewCheckCompiler[review_check_compiler]
    reviewCheckCompiler --> reviewCheckValidator[review_check_validator]
    reviewCheckValidator -->|log_only| generalCritiquer
    reviewCheckValidator -->|enforced| reviewCheckContextPlanner[review_check_context_planner]
    reviewCheckContextPlanner --> reviewCheckFocusedContext[review_check_focused_context]
    reviewCheckFocusedContext --> reviewCheckExecutor[review_check_executor]
    reviewCheckExecutor -->|more context needed| reviewCheckContextPlanner
    reviewCheckExecutor --> evidenceGate[review_check_evidence_gate]
    evidenceGate -->|optional scout| reviewCheckScout[review_check_scout]
    reviewCheckScout -->|emitted checks| reviewCheckExecutor
    reviewCheckScout -->|done| initialFocusedContext
    evidenceGate --> initialFocusedContext[initial_focused_context]
    generalCritiquer --> initialFocusedContext
    initialFocusedContext --> reviewEvidenceTriage[review_evidence_triage]
    reviewEvidenceTriage --> adversarialReflection[adversarial_reflection]

    adversarialReflection --> needsContext{Any routed reflection needs context?}
    needsContext -->|focused request| focusedContext[focused_context]
    needsContext -->|verification/revision needed| postReflectionEvidence[post_reflection_evidence_pass]
    focusedContext --> routeAfterFocused{Verifier or critique revision}
    postReflectionEvidence --> routeAfterFocused
    routeAfterFocused -->|Send eligible candidates| verifierSubgraph[verifier_subgraph]
    verifierSubgraph --> postVerifierGate[post_verifier_gate]
    postVerifierGate --> routeCritiqueRevision{Route critique revision}
    routeAfterFocused -->|no verifier work| routeCritiqueRevision
    routeCritiqueRevision -->|Send per shard| critiqueRevisionDigest[critique_revision_digest]
    critiqueRevisionDigest --> critiqueRevisionReduce[critique_revision_reduce]
    critiqueRevisionReduce --> reviewAdjudicator[review_adjudicator]
    routeCritiqueRevision -->|skip| reviewAdjudicator
    needsContext -->|no| reviewAdjudicator

    reviewAdjudicator --> reviewSynthesizer[review_synthesizer]
    reviewSynthesizer --> graphEnd([END])
```

## Mental Model And Planner Path

After Phase 2 `semantic_merge`, or when resuming from a loaded exploration
snapshot (`snapshot_source: "loaded"`), the graph runs the same mental-model path:
`intent_extractor`, `review_history_context`, `mandate_explorer`, `mandate_patch`,
actor-critic planning (`draft_planner`, `plan_critic`, `plan_revision`,
`mandate_finalize`, `plan_emit`), then `snapshot_pin`. The full `BehavioralSpec` is written to disk via
`BehavioralSpecStore`; only `behavioral_spec_ref` and `cache_refs` appear on
`GraphState`.

- `Settings.reviewer_check_mode` / CLI `--review-check-mode`:
  - **`enforced` (default):** The current check-first path. After `mental_model_context_enricher`, the graph runs `review_check_compiler`, validates checks, gathers check-focused context, executes checks, gates evidence-backed candidates, then continues through evidence triage, reflection, optional verifier, critique revision, and adjudication.
  - **`log_only`:** Compiles and validates review checks from the mental model, then still runs `general_critiquer`.
  - **`off`:** Candidate-first debug comparison after `mental_model_context_enricher`; the task proceeds to `general_critiquer`.

For current check-first benchmark runs, use:

```bash
scripts/cluster/submit_batch2_review_checks.sh enforced
```

or directly:

```bash
python -m src.reviewer_agent.main --remote --trace --pr-urls <url> [<url> ...]
```

For snapshot-resume runs using the same design:

```bash
python -m src.reviewer_agent.main --snapshot-id <snapshot-id> --pr-url <pr-url> --trace
```

### Adversarial Critique Pipeline

Each planned task runs the compiled subgraph `critique_review_subgraph`:

`critique_context_probe` -> `mental_model_context_enricher` -> either `general_critiquer` or the check-first review-check chain, depending on `reviewer_check_mode`.

This ensures direct code context is gathered **before** optional `query_mental_model` calls. In `enforced` mode, contract questions and surface invariants from the `BehavioralSpec` are converted into concrete review checks; the validator routes through `review_check_context_planner` and `review_check_focused_context` before execution, and the evidence gate can run a bounded `review_check_scout` loop before ending the branch. Parallel `Send` payloads use `payload_for_send` to avoid copying forbidden large keys.

### Exploration Ledger

`exploration_ledger` is append-only (`operator.add`). Prompts include only a **bounded** digest via `format_exploration_ledger_for_prompt` in `src/orchestration/prompts/ledger_formatter.py` so reflection and critiquer prompts do not inline the full ledger.

### Snapshot Resume (`snapshot_source: "loaded"`)

When the harness loads a prior exploration snapshot (for example `reviewer-agent --snapshot-id`), Phase 2 community agents still do not re-run. The graph still routes through `intent_extractor`, `mandate_explorer`, `mandate_patch`, actor-critic planning, and `snapshot_pin`. For loaded snapshots, **`snapshot_pin` does not call `SnapshotWriter.write_snapshot` again** (no duplicate tree); it merges `behavioral_spec_ref` (and ids) into `metadata["exploration_snapshot"]` and logs `snapshot_pin:loaded_passthrough`.

## Routing And State

The graph state type is `GraphState` in `src/domain/state.py`. Important channels:

- Inputs: `run_id`, `repo_path`, `git_diff`.
- Documentation pre-brief: `docs_prebrief_summary`, `docs_prebrief_sources`.
- Preflight and structural context: `diff_manifest_ref`, `preflight_summary`, `preflight_errors`, `preflight_warnings`, `structural_graph_node_link`, `structural_topology`, `structural_extraction_gaps`.
- Planning state: `root_task_id`, `task_registry`, `task_status_by_id`.
- Adversarial review state: `candidate_findings`, `review_checks`, `review_check_results`, `invalid_review_checks`, `reflection_reports`, `focused_context_requests`, `focused_context_results`, `critique_revision_digests` (map-step outputs merged by shard id).
- Runtime verifier: `source_facts`, `verifier_candidate`, `verifier_reports`.
- Mental model: `behavioral_spec_ref` (pointer only), `exploration_ledger` (bounded entries for `query_mental_model` and metrics).
- Outputs: `findings` and `final_findings`.
- Debugging: `metadata`, `node_history`, `token_usage`.

Parallel fan-out relies on reducers:

- List channels such as `candidate_findings`, `reflection_reports`, `findings`, `exploration_ledger`, and `node_history` use `operator.add`.
- Dict channels such as `task_registry`, `task_status_by_id`, `focused_context_results`, and `critique_revision_digests` use dict union reducers.
- `metadata` uses `merge_graph_metadata`, a recursive dict merge. This is required because multiple parallel `general_critiquer` nodes update `metadata` in the same LangGraph step.

## Preflight And Structural Extraction

Preflight currently happens inside the structural extraction nodes, not as a separate graph node.

### Local Repository Path

If `repo_path` points to a local directory, `_route_initial_context` sends the run to `structural_extractor`.

`structural_extractor`:

1. Calls `preflight_service.build_diff_manifest(...)` with `PreflightRequest(run_metadata, raw_diff)`.
2. Passes the resulting manifest into `StructuralGraphBuilder.build(...)`.
3. Uses the host-side AST parser when available.
4. Optionally runs structural topology / community detection.
5. Writes:
   - `diff_manifest_ref`
   - `preflight_summary`
   - `preflight_errors`
   - `preflight_warnings`
   - `structural_graph_node_link`
   - `structural_topology`
   - `structural_extraction_gaps`
   - structural metadata

### Remote Repository Path

If `repo_path` is not a local directory, `_route_initial_context` sends the run to `sandbox_structural_extractor`.

`sandbox_structural_extractor`:

1. Calls the same `preflight_service.build_diff_manifest(...)` over the raw diff.
2. Starts or reuses a sandbox checkout through `LazyReviewContextProvider`.
3. Extracts entities inside the sandbox via `collect_structural_entities(...)`.
4. Builds the structural graph on the host from those extracted entities.
5. Optionally runs structural topology / community detection.
6. Writes the same preflight and structural state fields as the local extractor.

For remote runs, `RunMetadata.base_sha` is currently set to `"unknown"` and `head_sha` to `run_id` in the extraction node. The harness has PR metadata and commits in the dataset, but that commit metadata is not yet wired into preflight run metadata in this path.

## Task Planning

The graph drafts and critiques tasks with `draft_planner`, `plan_critic`, `plan_revision`, and `plan_emit`, using the `BehavioralSpec` as the main contract source.

Inputs:

- Changed files from the diff, falling back to file nodes in `structural_graph_node_link`.
- `preflight_summary`.
- Derived structural routing hints, not the raw topology payload.
- Global insights.
- A bounded diff excerpt.

Important behavior:

- The LLM planner is asked to return a flat `tasks` array.
- `_normalize_tasks(...)` still defensively flattens nested `subtasks`, because local models may return hierarchical plans anyway.
- Each executable task is stored in `task_registry`.
- The root task is stored separately as `review-root`.
- Each leaf task starts with `task_status_by_id[task.id] = "pending"`.

The planner no longer passes raw `structural_topology.model_dump()` into the prompt, because that can include thousands of node IDs and blow up model context. Instead, it uses planner-specific `Structural Routing Hints` derived from changed file neighborhoods.

## Task Review Fan-Out

`_route_critique_tasks` reads `task_registry` and emits one LangGraph `Send("critique_review_subgraph", payload)` per pending leaf task, excluding `root_task_id`.

Each subgraph branch:

1. Loads the task identified by `current_task_id`.
2. Collects direct context with `critique_context_probe`.
3. Pulls task-scoped BehavioralSpec and Review KB excerpts in `mental_model_context_enricher`.
4. In the default `reviewer_check_mode=enforced`, compiles and executes concrete review checks, then promotes only evidence-gated candidates.
5. Optionally asks `review_check_scout` for more checks when evidence suggests a gap.
6. If `reviewer_check_mode=off`, prompts `general_critiquer` to produce `CandidateFinding` objects for debug comparison.
7. Marks the task completed in `task_status_by_id`.

Execution continues through `initial_focused_context` (same bounded fulfiller as post-reflection focused context), then `review_evidence_triage`, before `adversarial_reflection`.

In candidate-first debug mode, the general critiquer is also responsible for routing each candidate to one or more reflector domains through `CandidateFinding.reflection_specialties`. Most findings should route to exactly one domain. Cross-domain findings can route to multiple domains. In the default check-first mode, candidates are produced by the review-check executor and then pass through the same downstream reflection/adjudication stages.

Current limitation: `CritiquerOutput.initial_focus_requests` are recorded in metadata but are not emitted into `focused_context_requests`. The active focused-context cycle is driven by reflection reports, not by initial critiquer requests.

## Adversarial Reflection

`adversarial_reflection` no longer sends every candidate to every reflector. It groups candidates by `reflection_specialties`:

- `security`
- `logic`
- `performance`
- `general`

Fallback routing:

- If `reflection_specialties` is non-empty, it is authoritative.
- If empty, `suspected_category` is used when it matches a reflector domain.
- Otherwise the candidate routes to `general`.

Each reflector receives only candidates routed to that domain and returns `ReflectionReport` objects. This reduces cost and prevents off-domain vetoes from suppressing valid findings.

Reflection verdicts:

- `accept`
- `reject`
- `needs_context`
- `reclassify`
- `not_applicable`

If a routed domain expert returns `not_applicable`, cleanup records the candidate as misrouted and drops it. This is intentional: it exposes cases where the general critiquer assigned the wrong domain.

## Focused Context And Critique Revision (Map / Reduce)

After reflection, `_route_focused_after_reflection` checks for any `ReflectionReport` with:

- `verdict == "needs_context"`
- a non-null `focused_request`

If none exist, the graph goes directly to `review_adjudicator`.
If any report asks for `needs_verification`, or if revision candidates exist without a second focused request, the graph routes through `post_reflection_evidence_pass` so source-only verifier facts, runtime verifier fan-out, and critique revision can still run.

If any exist:

1. `focused_context` deduplicates and fulfills structured requests.
2. Requests are bounded by caps in `src/orchestration/context/review_context.py`:
   - max files per request: `5`
   - max text queries: `5`
   - max symbol queries: `5`
   - max search hits per query: `15`
   - max file slice chars: `8000`
   - max total result chars per fulfilled request: `24000`
3. The fulfiller can read file slices, run bounded text searches, return AST entity summaries when available, and add structural neighbor summaries.

Post-reflection revision is intentionally split so prompts cannot concatenate **every** focused-context payload for **every** candidate into one model call (that scaling bug dominated token usage on large PRs).

`_route_critique_revision` runs immediately after `focused_context`:

- If there are no reflection `needs_context` candidates, or there is no usable focused evidence for those candidates, it routes straight to `review_adjudicator`.
- Otherwise it builds deterministic **shards**: each shard is one `CandidateFinding` plus a character-budgeted subset of that candidate's `FocusedContextResult` rows. Shard ids are stable (`{candidate_id}:{shard_index}`) so parallel digest updates merge safely via `critique_revision_digests` using dict union.
- For each shard, the graph issues `Send("critique_revision_digest", payload)`, passing a transient `critique_revision_shard` payload for that invocation only.

The critique revision stage has two LLM nodes:

1. **`critique_revision_digest` (map)** — Condenses a single shard into compact bullets and an `impact` label (`supports` / `weakens` / `contradicts` / `unclear`). Prompt template: `critique_revision_digest.md`. Failures still emit a minimal digest with warnings so downstream cleanup can reason about coverage gaps.
2. **`critique_revision_reduce` (reduce)** — Consumes candidate summaries plus **only** the merged digests (not the raw focused-context blobs again). Emits `CritiqueRevisionOutput`, normalized so duplicate or unknown `candidate_id` entries become warnings instead of corrupting promotion. Prompt template: `critique_revision.md`. Writes `metadata["critique_revision"]`, including `revisions` for cleanup plus trace fields such as `candidate_count`, `shard_count_planned`, `digest_count`, `digest_shard_ids`, and `missing_digest_shards`.

Settings (`Settings` in `src/config.py`) tune shard sizing:

- `reviewer_critique_revision_max_shard_chars` — approximate serialized JSON budget per digest shard.
- `reviewer_critique_revision_max_candidate_chars` — truncation guard when inlining candidate JSON into prompts.

This is still a **single** focused-context cycle after reflection (no recursion back through reflection).

## Review Evidence Triage And Verifier

`review_evidence_triage` runs after initial focused context and before adversarial reflection. It normalizes each candidate's claim family, recommended reflector specialties, source-fact requests, runtime-verification usefulness, and additional needed context. If the triage LLM is unavailable, deterministic fallback triage uses the candidate's declared fields.

Verifier routing sits after post-reflection focused context or the `post_reflection_evidence_pass` bridge. It can:

- collect cheap source-only facts when enabled,
- fan out eligible candidates through `verifier_subgraph`,
- merge `verifier_reports` at `post_verifier_gate`,
- pass verifier advisories into critique revision and adjudication metadata.

The verifier remains advisory. Promotion is still decided by reflection, critique revision, and `review_adjudicator`; verifier data can strengthen or weaken the evidence packet but does not independently publish findings.

## Adjudication And Synthesis

`review_adjudicator` makes the final candidate judgment and promotes supported `CandidateFinding` objects into clean `ReviewFinding` objects.

Current promotion rules:

- Determine final category from candidate `suspected_category` plus relevant `reclassify` reports.
- Relevant reflectors are the candidate's routed `reflection_specialties`; if absent, the final category determines relevance.
- `reject` only blocks promotion when it comes from a relevant reflector.
- `needs_context` only blocks when it comes from a relevant reflector and focused context / revision does not support the candidate.
- Off-domain `reject` and `needs_context` reports are recorded in metadata and ignored for promotion.
- Relevant `not_applicable` reports mark the candidate as misrouted and drop it.
- Promoted findings become `findings`.

`review_synthesizer` then deduplicates and sorts `findings` into `final_findings`.

## What Is Not Complete Yet

### Preflight Is Not A First-Class Graph Node

Preflight is embedded inside structural extraction. That means it cannot yet independently:

- fail fast before structural extraction,
- be cached and resumed separately,
- be inspected as its own graph stage,
- run without also building structural context.

The graph can skip extraction if both `preflight_summary` and `structural_graph_node_link` are already present, but there is no dedicated `preflight` node that materializes a full preflight artifact into state.

### Diff Manifest Is Referenced, Not Stored

The state stores `diff_manifest_ref`, `preflight_summary`, errors, and warnings. It does not store the full `DiffManifest` object. Planner and downstream nodes therefore only see the summary and changed-file extraction helpers, not full hunk-level preflight structure.

### Preflight Metadata Is Incomplete For Remote Runs

Remote structural extraction currently constructs `RunMetadata` with:

- `base_sha = "unknown"`
- `head_sha = run_id`

For AACR / GitHub PR runs, the harness knows more precise PR metadata, including base/head commits from the dataset, but that metadata is not wired into the preflight request in the graph.

### Planner Uses Limited Preflight Signals

The planner receives only `PreflightSummary`, changed files, structural routing hints, global insights, and a diff excerpt. It does not yet consume:

- per-file preflight metrics,
- hunk boundaries,
- ambiguity flags,
- risk hints,
- parse issue details beyond summary booleans.

This limits how well preflight can guide task decomposition.

### Focused Context Does Not Use Preflight Hunks Directly

Focused context can read file prefixes/slices and run bounded search, but it does not yet use preflight hunk coordinates to retrieve exact changed regions or neighboring unchanged lines. Context gathering is therefore still file-oriented rather than hunk-oriented.

### Initial Critiquer Context Requests Are Not Executed

`CritiquerOutput` supports `initial_focus_requests`, but `general_critiquer` currently records these in metadata and returns an empty `focused_context_requests` list. Only reflection-driven `needs_context` requests are fulfilled.

### Raw Artifacts Are Better But Not Complete Provenance

The harness writes candidates, reflections, focused requests, focused results, critique revision digests (when present), worker reports, and metadata. It does not yet write a complete preflight manifest or exact prompt payloads / prompt sizes for each node.

## Operational Notes

- Redis checkpointing is used when `redis_enabled` is true. If Redis checkpointing fails, `run_reviewer` falls back to an in-memory graph run.
- `docs_prebrief` runs before initial structural routing when enabled and not already marked done.
- `review_history_context` can add bounded prior PR review/comment history before mandate planning when GitHub MCP review history is enabled.
- The same `LazyReviewContextProvider` is shared across the graph run and is stopped by the wrapper around `review_synthesizer`.
- Local LLM model keys come from `Settings.reviewer_planner_model_key` and `Settings.reviewer_worker_model_key` (used by critiquer, reflection, digest, and reduce nodes).
- Critique revision shard sizing uses `Settings.reviewer_critique_revision_max_shard_chars` and `Settings.reviewer_critique_revision_max_candidate_chars`.
