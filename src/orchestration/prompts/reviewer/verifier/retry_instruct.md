# Retry hint

The previous verification script did not yield a conclusive product-behavior result.

## Failure class

{error_class}

## Target file (cited defect)

{target_file_path}

## Prior attempts (summary)

{prior_attempts_summary}

## Repeat warning

{repeat_hint}

## Suggested fix

{action_hint}

## Last exit code

{exit_code}

## Stdout (truncated)

{stdout_tail}

## Stderr (truncated)

{stderr_tail}

Produce an improved standalone script that follows the same STATUS contract (SAFE / CRASHED / MISMATCH / HARNESS_ERROR + exit codes).

- Use `inspect.signature` or read `INPUT_TYPES` from the **actual** module before calling `execute`.
- Stub only what the traceback requires (no fixed framework enum tables).
- If the disputed fact is a standard-library operation already visible in supplied source, replace
  a failing whole-module import with a tiny isolated repro of that exact operation.
- After a missing-module or import-chain failure, do not keep stubbing the expanding dependency
  graph. The next attempt must either exercise the smallest real source fragment/stdlib operation
  that decides the claim, or report `STATUS: HARNESS_ERROR` because runtime verification is not
  appropriate for this candidate.
- Use `STATUS: HARNESS_ERROR` for import/setup/syntax failures; reserve `STATUS: CRASHED` for exceptions **after** the product under test is loaded and invoked.

Return **only** Python source.
