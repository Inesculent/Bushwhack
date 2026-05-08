You are resolving an unverified cross-community call target for a code-review graph.
Return structured JSON matching ResolverSymbolSummaryOutput.

Task:
- Write exactly one sentence that states this symbol's observable responsibility.
- Mention side effects, returned data, external calls, or mutation only if visible in the body.
- Keep the sentence useful for deciding whether callers need deeper review.

Evidence rules:
- Use only the supplied symbol body.
- Do not speculate about callers, hidden dependencies, or runtime behavior not shown.
- Preserve the provided symbol_node_id exactly.

symbol_node_id={symbol_node_id}
Symbol body:
```
{body_text}
```
