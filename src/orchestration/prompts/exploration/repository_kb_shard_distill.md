You distill one or more bounded Repository KB evidence shards.
Return structured JSON matching RepositoryKBShardDistillationOutput.

Task:
- Summarize each shard as reusable repository knowledge.
- Preserve the lane focus: files, bridge symbols, contracts/facts, dependency edges, or internal symbols.
- Cite only record ids present in each shard's `allowed_record_ids`.
- Do not inventory every record. Cluster similar symbols/facts and summarize the lane's role.

Evidence rules:
- Use only the supplied KB records.
- Do not infer hidden implementation details or invent files, symbols, bugs, callers, or behavior.
- Prefer specific contracts, signatures, shapes, config surfaces, dependency boundaries, and retrieval hints.
- Shard summaries are intermediate evidence views; they should be rich enough for a later community merge.
- Output budget per shard: summary under 120 words; at most 4 items for each list field; each item under 18 words; at most 12 source_record_ids; no prose outside the JSON schema.

Repository KB shard pack:
{pack_json}
