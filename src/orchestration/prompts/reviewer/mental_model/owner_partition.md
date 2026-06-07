# Owner Context Partitioner

Partition changed owners into focused context groups for later contract-question generation.

This is context delegation only. Do not infer defects, risks, or recommendations.

Rules:
- Default to one executable owner per group with its matching class/schema/helper companions.
- Group multiple primary owners only when the owner cards show direct coupling: shared operation, caller/callee relation, registration/integration contract, or one owner's output consumed by another.
- Keep unrelated same-file owners separate even when they share broad PR intent.
- Use compact reasons that name the coupling or say the owner is independent.
- Mark complexity as `complex` only when the owner cards show transformation chains, branching variants, aggregation/serialization, indexing/projection, resource/lifecycle ordering, or integration coupling.

Return structured fields matching `OwnerPartitionOutput`.
