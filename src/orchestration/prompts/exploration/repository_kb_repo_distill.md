You distill bounded Repository KB records into a repository-understanding brief.
Return structured JSON matching RepositoryKBRepoDistillationOutput.

Task:
- Explain what this repository is for and what a reviewer should understand before reading code.
- Identify main user/runtime workflows, domain concepts, runtime model, extension/plugin/API surfaces, and cross-subsystem contracts.
- Explain important shapes, configs, APIs, invariants, review mental-model cues, and failure-cascade boundaries.
- Corroborate or qualify the Repository KB against docs_context when docs are present.
- Cite only record ids present in `allowed_record_ids`.
- Cite only doc ids present in `docs_context.allowed_doc_source_ids`.

Evidence rules:
- Use only the supplied KB records.
- Treat docs_context as PR-agnostic repository documentation, not proof of exact behavior.
- Do not include PR-specific changed-file bias.
- Do not introduce findings or bugs; this is reusable repository understanding.
- Summaries are navigation context and must point back to KB record ids.
- Static graph topology is supporting context; use it to highlight boundary/cascade routes, not to re-list dependency flow.
- Keep the summary compact enough for downstream prompts, but richer than a topology map.

Repository KB pack:
{pack_json}
