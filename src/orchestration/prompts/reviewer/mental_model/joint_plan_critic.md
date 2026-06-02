# Joint Mandate + Plan Critic

Evaluate **draft review tasks** against the behavioral mandate **and** whether the mandate itself needs more repo evidence.

## Bootstrap digest (one-liner)

{bootstrap_digest_oneliner}

## Behavioral mandate excerpt

{behavioral_mandate_excerpt}

## Draft tasks JSON

{draft_tasks_json}

## Mission

Set `aligned=true` only when:
- tasks cover mandate contracts, risk hypotheses, and diff-local correctness; AND
- the mandate is adequate for this PR (bootstrap exploration already ran); AND
- **scope completeness** holds (see below).

Set `aligned=false` when tasks miss coverage OR the mandate lacks evidence for a critical area.

### Scope completeness gate

Set `aligned=false` if any of the following apply:

- A surface listed in **Surface ledger (JSON)** (or a distinct feature in **PR context**) has **no** task coverage - neither a dedicated task nor inclusion in a **diff-local correctness** `logic` task that names the surface set and declares its `surface_ids`.
- A task mentions changed surfaces but omits matching `surface_ids` from **Surface ledger (JSON)**.
- Two non-cross-surface `logic` tasks claim the same `surface_ids`; require disjoint logic scopes unless the task explicitly says it is cross-surface/integration.
- Handlers with multiple modes/discriminants or multi-path control flow only have security or performance tasks and **no** `logic` task that audits implementation correctness (not merely input-boundary speculation).
- **One** `logic` task that only says "audit all N handlers" without splitting work when **Surface ledger (JSON)** has **8+** entry points - set `aligned=false` and instruct the reviser to emit **two or more** `logic` tasks with disjoint surface subsets. If a surface has its own changed contract evidence, ask for a focused contract task for that surface; do not prescribe an issue class.
- The bootstrap digest **Surfaces** segment (when present) is inconsistent with the surface inventory (e.g. clearly fewer surfaces named than in the inventory).

When the fix is task wording or redistribution across specialties, prefer `aligned=false` with clear `revision_instructions` over `exploration_requests`.

"Adequate" favors breadth of surface coverage over perfect depth on one component.

## Exploration requests

When `mandate_adequate=false` or tasks need facts from code not in the mandate, emit concrete `exploration_requests` (file_path, symbol, question). Do not request exploration for issues fixable by revising tasks alone.

## Output

Return `JointPlanCritiqueOutput` with `aligned`, `gaps`, `revision_instructions`, `mandate_adequate`, `exploration_requests`.
