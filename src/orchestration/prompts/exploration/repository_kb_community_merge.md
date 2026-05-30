You merge Repository KB shard summaries into a rich community-level summary.
Return structured JSON matching RepositoryKBCommunityDistillationOutput.

Task:
- Produce one reusable repository summary for each community in the pack.
- Synthesize responsibilities, public contracts, bridge symbols, important facts, data-shape/config/API notes, risk surfaces, uncertainties, and retrieval hints.
- Cite shard summary ids and any direct KB ids present in `allowed_record_ids`.

Evidence rules:
- Use only the supplied shard summary records and community metadata.
- Do not treat omitted coverage as evidence; mention uncertainty or retrieval hints for omitted areas.
- Do not introduce PR-specific changed-file bias.
- Keep output concise, but not laconic: preserve the facts a reviewer would need to navigate the subsystem.

Repository KB community merge pack:
{pack_json}
