# Reviewer Engine Expectation

The reviewer engine should behave as a staged evidence system, not as a diff
scanner or a memorized issue matcher. Its purpose is to understand what changed,
identify the behavioral contracts around that change, and decide whether the
implementation preserves those contracts.

## Core Model

Every review decision should keep three questions separate:

1. What changed?
   The changed files, owners, branches, modes, schemas, return paths, data flows,
   integration points, and reachable behavior.

2. Why does the behavior matter?
   The contract source: PR intent, old behavior, API shape, schema declaration,
   caller expectation, framework rule, repository convention, documentation,
   test pattern, or data representation invariant.

3. Does the implementation satisfy it?
   The concrete code evidence showing branch behavior, value preservation,
   cardinality, type closure, error behavior, lifecycle order, integration use,
   or downstream effects.

A finding needs all three: a concrete operation, a concrete expected behavior, a
concrete violation, and plausible impact. A suppression needs the same
discipline. It is not enough to say the code contains a guard, branch, join,
type declaration, or local implementation mechanic. The suppression must show
that the mechanic preserves the contract the check is actually asking about.

## Pipeline Expectations

The mental-model and repository-understanding phases should produce reusable
contract hypotheses, not only summaries. A useful hypothesis identifies the
changed surface, the suspected contract, the source of that contract, evidence
needed to validate or reject it, and whether that evidence is local or requires
repository context.

The planner should use those hypotheses for broad coverage across changed
surfaces. It should prefer behavior-shaped tasks and lenses over narrow
benchmark-specific issue forms.

The compiler should turn tasks into checks that preserve contract uncertainty.
When a check depends on a convention, schema, data representation, caller,
downstream path, framework rule, mode, aggregation, or selection behavior, the
check must require evidence for why the expected behavior is the correct
contract. It should not ask the executor to rediscover broad context from
scratch.

The focused-context stage should retrieve missing contract evidence when local
implementation evidence is insufficient. "The implementation is visible but the
contract justification is missing" is a meaningful state, not a safe answer.

The executor should only suppress when both conditions hold:

- implementation evidence shows the code performs the expected behavior;
- contract evidence explains why that behavior is correct for the surrounding
  schema, caller, convention, representation, or integration contract.

If only implementation evidence exists, the executor should return
`unsupported`, request focused context when budget remains, or produce a cautious
candidate only when the violation is already concrete and contract-backed.

The adjudicator is final quality control. It should not be expected to recover
issues that never became candidates because the compiler asked the wrong
question or the executor over-suppressed a missing-justification check.

## Failure Diagnostics

Misses should be diagnosable by pipeline stage:

- surface not planned or assigned;
- contract source not retrieved;
- compiled check changed polarity or drifted to a neighboring invariant;
- focused context could not provide the requested justification;
- executor suppressed based on implementation mechanics alone;
- adjudicator dropped a valid candidate.

The benchmark is an imperfect measurement surface. The engine should optimize
for general behavior: broad surface coverage, contract-grounded checks,
justification-aware suppression, and useful diagnostics when one of those pieces
is missing.
