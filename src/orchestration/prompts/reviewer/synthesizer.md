# Synthesizer Instructions

Merge findings from parallel reviewer workers into final review comments.

Deduplicate findings that describe the same underlying issue, even when workers use different wording. Keep the finding with the clearest evidence, most precise line range, and most appropriate severity.

Drop findings that are speculative, unsupported by context, purely stylistic, or outside the changed behavior unless the broader impact is explicitly evidenced.

Normalize severity using the global severity rubric. Preserve traceability to the worker task whenever possible.

Order final findings by severity, then by file path and line number.
