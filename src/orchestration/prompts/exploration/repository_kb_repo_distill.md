You distill bounded Repository KB summary records into a repository-level map.
Return structured JSON matching RepositoryKBRepoDistillationOutput.

Task:
- Describe the repository's current checked-out state at a high level.
- Identify top subsystems, public contracts, dependency flow, risk surfaces, and uncertainties.
- Cite only record ids present in `allowed_record_ids`.

Evidence rules:
- Use only the supplied KB records.
- Do not include PR-specific changed-file bias.
- Do not introduce findings or bugs; this is reusable repository understanding.
- Summaries are navigation context and must point back to KB record ids.
- Keep the summary compact enough for downstream prompts.

Repository KB pack:
{pack_json}
