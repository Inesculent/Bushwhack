import re
from pathlib import Path

from src.config import Settings
from src.domain.schemas import (
    CodeEntity,
    RepositoryKBCommunityDistillationOutput,
    RepositoryKBCommunityDistillationItem,
    RepositoryKBRepoDistillationOutput,
    RepositoryKBShardDistillationOutput,
    RepositoryKBShardDistillationItem,
    SnapshotDiagnostics,
    StructuralTopologyCommunity,
    StructuralTopologySummary,
)
from src.infrastructure.review_kb_distillation import (
    RepositoryKBDistillationPlanner,
    build_community_distillation_pack,
    build_repo_distillation_pack,
    community_summary_record_from_distillation,
    repo_summary_record_from_distillation,
)
from src.infrastructure.review_kb import (
    build_review_kb,
    compatibility_summaries_from_kb,
    load_review_kb,
    query_loaded_review_kb,
)
from src.orchestration.nodes.exploration.repository_kb_distillation import distill_repository_kb
from src.infrastructure.snapshot_writer import SnapshotWriter
from src.infrastructure.structural_graph import StructuralGraphBuilder
from src.tools.review_kb_tools import query_repository_kb, query_review_kb


def _fixture_graph(tmp_path: Path):
    train_path = "comfy_extras/nodes_train.py"
    lora_path = "comfy/weight_adapter/lora.py"
    (tmp_path / "comfy_extras").mkdir()
    (tmp_path / "comfy" / "weight_adapter").mkdir(parents=True)
    (tmp_path / train_path).write_text(
        "def train_node(x):\n"
        "    return load_lora(x)\n",
        encoding="utf-8",
    )
    (tmp_path / lora_path).write_text(
        "# Expected tensor shape: input [batch, channels, height, width]\n"
        "def load_lora(tensor, rank: int):\n"
        "    return tensor\n",
        encoding="utf-8",
    )
    result = StructuralGraphBuilder.build_from_entities(
        entities_by_file={
            train_path: [
                CodeEntity(
                    name="train_node",
                    type="function",
                    signature="def train_node(x):",
                    body="def train_node(x):\n    return load_lora(x)",
                    definition_line=1,
                )
            ],
            lora_path: [
                CodeEntity(
                    name="load_lora",
                    type="function",
                    signature="def load_lora(tensor, rank: int):",
                    body="def load_lora(tensor, rank: int):\n    return tensor",
                    definition_line=2,
                )
            ],
        },
        file_languages={train_path: "Python", lora_path: "Python"},
    )
    payload = StructuralGraphBuilder.serialize(result.graph)
    node_to_community = {}
    communities = []
    for cid, prefix in [(0, train_path), (1, lora_path)]:
        nodes = [
            str(node["id"])
            for node in payload["nodes"]
            if str(node.get("file_path") or "") == prefix
        ]
        for node_id in nodes:
            node_to_community[node_id] = cid
        communities.append(
            StructuralTopologyCommunity(
                community_id=cid,
                node_ids=nodes,
                cohesion=0.8,
                file_count=1,
                symbol_count=1,
            )
        )
    topo = StructuralTopologySummary(
        algorithm="fixture",
        community_count=2,
        communities=communities,
        node_to_community=node_to_community,
    )
    return payload, topo


def _massive_fixture_graph(tmp_path: Path, *, file_count: int = 18):
    entities_by_file = {}
    languages = {}
    for idx in range(file_count):
        path = f"pkg/mod_{idx}.py"
        (tmp_path / "pkg").mkdir(exist_ok=True)
        (tmp_path / path).write_text(
            f"# Contract: mod_{idx} expects tensor shape [batch, channels]\n"
            f"def func_{idx}(tensor, config):\n"
            f"    return tensor\n",
            encoding="utf-8",
        )
        entities_by_file[path] = [
            CodeEntity(
                name=f"func_{idx}",
                type="function",
                signature=f"def func_{idx}(tensor, config):",
                body=f"def func_{idx}(tensor, config):\n    return tensor",
                definition_line=2,
            )
        ]
        languages[path] = "Python"
    result = StructuralGraphBuilder.build_from_entities(
        entities_by_file=entities_by_file,
        file_languages=languages,
    )
    payload = StructuralGraphBuilder.serialize(result.graph)
    node_ids = [str(node["id"]) for node in payload["nodes"]]
    topo = StructuralTopologySummary(
        algorithm="fixture",
        community_count=1,
        communities=[
            StructuralTopologyCommunity(
                community_id=0,
                node_ids=node_ids,
                cohesion=0.5,
                file_count=file_count,
                symbol_count=file_count,
            )
        ],
        node_to_community={node_id: 0 for node_id in node_ids},
    )
    return payload, topo


def test_review_kb_builds_profiles_and_dependency_query(tmp_path: Path) -> None:
    payload, topo = _fixture_graph(tmp_path)

    bundle = build_review_kb(
        run_id="r1",
        repo_path=str(tmp_path),
        graph_payload=payload,
        topology=topo,
        changed_file_paths={"comfy_extras/nodes_train.py"},
    )

    assert bundle.manifest.counts["files"] == 2
    assert bundle.manifest.counts["symbols"] == 2
    assert bundle.manifest.counts["summaries"] == 3
    assert bundle.manifest.diagnostics["kb_scope"] == "repository"
    assert bundle.review_overlay["changed_files"] == ["comfy_extras/nodes_train.py"]
    assert any("tensor-shape" in fact.tags for fact in bundle.facts)
    assert all(r.kind == "summary" for r in bundle.summaries)
    assert all(r.metadata.get("source_record_ids") for r in bundle.summaries)

    loaded = {
        "repo": bundle.repo,
        "communities": bundle.communities,
        "files": bundle.files,
        "symbols": bundle.symbols,
        "facts": bundle.facts,
        "edges": bundle.edges,
        "summaries": bundle.summaries,
        "lexical_index": bundle.lexical_index,
        "by_id": {
            record.id: record
            for record in [
                bundle.repo,
                *bundle.communities,
                *bundle.files,
                *bundle.symbols,
                *bundle.facts,
                *bundle.edges,
                *bundle.summaries,
            ]
        },
    }
    result = query_loaded_review_kb(
        loaded,
        query="LoRA adapter expected tensor shapes and method signatures",
        path="comfy_extras/nodes_train.py",
        topics=["lora", "tensor-shape", "signature"],
        max_results=10,
    )

    summaries = "\n".join(
        record.summary for record in [*result.primary_records, *result.related_records]
    ).lower()
    assert "load_lora" in summaries
    assert "tensor shape" in summaries

    community_zero = query_loaded_review_kb(
        loaded,
        query="training community",
        community_id=0,
        max_results=4,
    )
    assert any(r.id == "community:0" for r in community_zero.related_records)


def test_repository_kb_core_records_ignore_diff_overlay(tmp_path: Path) -> None:
    payload, topo = _fixture_graph(tmp_path)

    base = build_review_kb(
        run_id="r1",
        repo_path=str(tmp_path),
        graph_payload=payload,
        topology=topo,
    )
    with_overlay = build_review_kb(
        run_id="r1",
        repo_path=str(tmp_path),
        graph_payload=payload,
        topology=topo,
        changed_file_paths={"comfy_extras/nodes_train.py"},
    )

    assert [r.model_dump() for r in base.files] == [r.model_dump() for r in with_overlay.files]
    assert [r.model_dump() for r in base.communities] == [r.model_dump() for r in with_overlay.communities]
    assert [r.model_dump() for r in base.summaries] == [r.model_dump() for r in with_overlay.summaries]
    assert base.manifest.coverage == with_overlay.manifest.coverage
    assert with_overlay.review_overlay["changed_files"] == ["comfy_extras/nodes_train.py"]


def test_repository_kb_distillation_pack_caps_and_omits_raw_bodies(tmp_path: Path) -> None:
    payload, topo = _fixture_graph(tmp_path)
    bundle = build_review_kb(
        run_id="r1",
        repo_path=str(tmp_path),
        graph_payload=payload,
        topology=topo,
    )

    base_pack = build_community_distillation_pack(bundle, 1, max_files=1, max_symbols=1, max_facts=1, max_edges=1)
    overlay_bundle = build_review_kb(
        run_id="r1",
        repo_path=str(tmp_path),
        graph_payload=payload,
        topology=topo,
        changed_file_paths={"comfy_extras/nodes_train.py"},
    )
    overlay_pack = build_community_distillation_pack(overlay_bundle, 1, max_files=1, max_symbols=1, max_facts=1, max_edges=1)

    assert len(base_pack["records"]) <= 5
    assert "return tensor" not in str(base_pack)
    assert base_pack == overlay_pack
    assert build_community_distillation_pack(bundle, 0)["records"]


def test_repository_kb_planner_splits_massive_community_without_silent_truncation(tmp_path: Path) -> None:
    payload, topo = _massive_fixture_graph(tmp_path)
    bundle = build_review_kb(
        run_id="r1",
        repo_path=str(tmp_path),
        graph_payload=payload,
        topology=topo,
    )
    planner = RepositoryKBDistillationPlanner(max_prompt_chars=1800, max_shards_per_community=100)

    plan = planner.plan(bundle, 0)

    assert plan.mode == "sharded"
    assert len(plan.shards) > 1
    assert all(shard.prompt_chars <= 1800 for shard in plan.shards)
    shard_source_ids = {rid for shard in plan.shards for rid in shard.source_record_ids}
    direct_ids = set(build_community_distillation_pack(bundle, 0, max_files=1000, max_symbols=1000, max_facts=1000, max_edges=1000)["allowed_record_ids"])
    assert direct_ids <= shard_source_ids
    assert plan.telemetry["shard_count"] == len(plan.shards)


def test_repository_kb_planner_records_omitted_shards_when_capped(tmp_path: Path) -> None:
    payload, topo = _massive_fixture_graph(tmp_path)
    bundle = build_review_kb(
        run_id="r1",
        repo_path=str(tmp_path),
        graph_payload=payload,
        topology=topo,
    )
    planner = RepositoryKBDistillationPlanner(max_prompt_chars=1800, max_shards_per_community=1)

    plan = planner.plan(bundle, 0)

    assert plan.mode == "sharded"
    assert len(plan.shards) == 1
    assert plan.telemetry["omitted_record_ids"]


def test_repository_kb_distillation_validates_citations(tmp_path: Path) -> None:
    payload, topo = _fixture_graph(tmp_path)
    bundle = build_review_kb(
        run_id="r1",
        repo_path=str(tmp_path),
        graph_payload=payload,
        topology=topo,
    )
    fallback = [r for r in bundle.summaries if r.id == "summary:community:1"][0]
    pack = build_community_distillation_pack(bundle, 1)

    record = community_summary_record_from_distillation(
        fallback=fallback,
        item=RepositoryKBCommunityDistillationItem(
            community_id=1,
            label="LoRA Adapter",
            purpose="Owns LoRA adapter loading contracts.",
            public_contracts=["load_lora requires a tensor and rank"],
            source_record_ids=[pack["allowed_record_ids"][0], "missing:id"],
        ),
        allowed_record_ids=pack["allowed_record_ids"],
        omitted=pack["omitted"],
    )

    assert record.confidence == "llm_synthesized"
    assert record.metadata["source_record_ids"] == [pack["allowed_record_ids"][0]]
    assert record.metadata["llm_distillation_status"] == "ok"
    assert "distillation_invalid_citations:1" in record.metadata["llm_distillation_warnings"]


def test_repository_kb_repo_distillation_uses_summary_pack(tmp_path: Path) -> None:
    payload, topo = _fixture_graph(tmp_path)
    bundle = build_review_kb(
        run_id="r1",
        repo_path=str(tmp_path),
        graph_payload=payload,
        topology=topo,
    )
    pack = build_repo_distillation_pack(bundle, max_community_summaries=1)
    fallback = [r for r in bundle.summaries if r.id == "summary:repo"][0]

    assert all(r["kind"] in {"repo", "summary"} for r in pack["records"])
    assert len([r for r in pack["records"] if r["kind"] == "summary"]) == 1

    record = repo_summary_record_from_distillation(
        fallback=fallback,
        output=RepositoryKBRepoDistillationOutput(
            summary="Repository organizes training and LoRA adapter contracts.",
            top_subsystems=["LoRA adapter"],
            source_record_ids=pack["allowed_record_ids"],
        ),
        allowed_record_ids=pack["allowed_record_ids"],
        omitted=pack["omitted"],
    )
    assert record.confidence == "llm_synthesized"
    assert "LoRA adapter" in record.summary


def test_repository_kb_hierarchical_distillation_preserves_failed_shards(monkeypatch, tmp_path: Path) -> None:
    payload, topo = _massive_fixture_graph(tmp_path)
    bundle = build_review_kb(
        run_id="r1",
        repo_path=str(tmp_path),
        graph_payload=payload,
        topology=topo,
    )
    calls = {"shards": 0}

    class FakeLlm:
        def __init__(self, schema):
            self.schema = schema

        def invoke(self, prompt: str):
            if self.schema is RepositoryKBShardDistillationOutput:
                calls["shards"] += 1
                shard_id = re.search(r'"shard_id": "([^"]+)"', prompt).group(1)
                lane = re.search(r'"lane": "([^"]+)"', prompt).group(1)
                if calls["shards"] == 1:
                    raise RuntimeError("boom")
                return RepositoryKBShardDistillationOutput(
                    shards=[
                        RepositoryKBShardDistillationItem(
                            community_id=0,
                            shard_id=shard_id,
                            lane=lane,
                            summary=f"{lane} shard summary",
                            retrieval_hints=[f"query {lane}"],
                            source_record_ids=["community:0"],
                        )
                    ]
                )
            if self.schema is RepositoryKBCommunityDistillationOutput:
                return RepositoryKBCommunityDistillationOutput(
                    communities=[
                        RepositoryKBCommunityDistillationItem(
                            community_id=0,
                            label="Merged",
                            purpose="Merged rich summary.",
                            data_shape_notes=["tensor shape [batch, channels]"],
                            retrieval_hints=["query shard contracts"],
                            source_record_ids=["summary:community:0:shard:overview_files:0"],
                        )
                    ]
                )
            return RepositoryKBRepoDistillationOutput(summary="Repo summary", source_record_ids=["repo"])

    def fake_worker(schema, **_kwargs):
        return FakeLlm(schema)

    monkeypatch.setattr(
        "src.orchestration.nodes.exploration.repository_kb_distillation.Models.worker",
        fake_worker,
    )

    distilled, _tokens, warnings = distill_repository_kb(
        bundle,
        settings=Settings(
            repository_kb_distillation_max_prompt_chars=2000,
            repository_kb_distillation_max_shards_per_community=8,
        ),
    )

    shard_summaries = [r for r in distilled.summaries if r.metadata.get("summary_scope") == "community_shard"]
    final = [r for r in distilled.summaries if r.id == "summary:community:0"][0]
    assert shard_summaries
    assert any(r.metadata.get("llm_distillation_status") == "failed" for r in shard_summaries)
    assert final.confidence == "llm_synthesized"
    assert "tensor shape" in final.summary
    assert any("failed" in w for w in warnings)
    loaded = {
        "repo": distilled.repo,
        "communities": distilled.communities,
        "files": distilled.files,
        "symbols": distilled.symbols,
        "facts": distilled.facts,
        "edges": distilled.edges,
        "summaries": distilled.summaries,
        "lexical_index": distilled.lexical_index,
        "by_id": {
            record.id: record
            for record in [
                distilled.repo,
                *distilled.communities,
                *distilled.files,
                *distilled.symbols,
                *distilled.facts,
                *distilled.edges,
                *distilled.summaries,
            ]
        },
    }
    result = query_loaded_review_kb(loaded, query="shard contracts", community_id=0, max_results=10)
    assert any(r.metadata.get("summary_scope") == "community_shard" for r in result.related_records)


def test_repository_kb_shard_length_failure_retries_compact(monkeypatch, tmp_path: Path) -> None:
    payload, topo = _massive_fixture_graph(tmp_path)
    bundle = build_review_kb(
        run_id="r1",
        repo_path=str(tmp_path),
        graph_payload=payload,
        topology=topo,
    )
    calls = {"shards": 0}

    class LengthFinishReasonError(Exception):
        pass

    class FakeLlm:
        def __init__(self, schema):
            self.schema = schema

        def invoke(self, prompt: str):
            if self.schema is RepositoryKBShardDistillationOutput:
                calls["shards"] += 1
                shard_id = re.search(r'"shard_id": "([^"]+)"', prompt).group(1)
                lane = re.search(r'"lane": "([^"]+)"', prompt).group(1)
                if calls["shards"] == 1:
                    raise LengthFinishReasonError("length limit was reached")
                assert "OUTPUT BUDGET" in prompt or calls["shards"] > 2
                return RepositoryKBShardDistillationOutput(
                    shards=[
                        RepositoryKBShardDistillationItem(
                            community_id=0,
                            shard_id=shard_id,
                            lane=lane,
                            summary="Compact shard summary.",
                            source_record_ids=["community:0"],
                        )
                    ]
                )
            if self.schema is RepositoryKBCommunityDistillationOutput:
                return RepositoryKBCommunityDistillationOutput(
                    communities=[
                        RepositoryKBCommunityDistillationItem(
                            community_id=0,
                            label="Merged",
                            purpose="Merged after retry.",
                            source_record_ids=["summary:community:0:shard:overview_files:0"],
                        )
                    ]
                )
            return RepositoryKBRepoDistillationOutput(summary="Repo summary", source_record_ids=["repo"])

    monkeypatch.setattr(
        "src.orchestration.nodes.exploration.repository_kb_distillation.Models.worker",
        lambda schema, **_kwargs: FakeLlm(schema),
    )

    distilled, _tokens, warnings = distill_repository_kb(
        bundle,
        settings=Settings(
            repository_kb_distillation_max_prompt_chars=2000,
            repository_kb_distillation_max_shards_per_community=8,
        ),
    )

    shard_summaries = [r for r in distilled.summaries if r.metadata.get("summary_scope") == "community_shard"]
    assert shard_summaries
    assert not any(r.metadata.get("llm_distillation_status") == "failed" for r in shard_summaries)
    assert any("retry:reason=length" in w for w in warnings)


def test_review_kb_compatibility_summaries_need_no_llm(tmp_path: Path) -> None:
    payload, topo = _fixture_graph(tmp_path)
    bundle = build_review_kb(
        run_id="r1",
        repo_path=str(tmp_path),
        graph_payload=payload,
        topology=topo,
    )

    summaries = compatibility_summaries_from_kb(bundle)

    assert {s.community_id for s in summaries} == {0, 1}
    assert all(s.symbol_summaries for s in summaries)
    assert summaries[0].purpose.startswith("Community 0")


def test_snapshot_writer_persists_review_kb_and_tool_queries_it(tmp_path: Path) -> None:
    payload, topo = _fixture_graph(tmp_path)
    settings = Settings(snapshot_base_path=tmp_path / "snapshots")
    writer = SnapshotWriter(settings)

    snap, root = writer.write_snapshot(
        run_id="kb-test",
        repo_path=str(tmp_path),
        enriched_graph_payload=payload,
        topology=topo,
        community_summaries=[],
        global_summary="summary",
        diagnostics=SnapshotDiagnostics(),
        unresolved_calls=[],
        extraction_gap_count=0,
        changed_file_paths={"comfy_extras/nodes_train.py"},
    )

    kb = load_review_kb(root)
    assert kb["manifest"].counts["files"] == 2
    assert Path(root, "review_kb", "symbols.jsonl").is_file()
    assert Path(root, "review_kb", "summaries.jsonl").is_file()
    assert Path(root, "review_kb", "review_overlay.json").is_file()
    assert snap.metadata["review_kb"]["counts"]["symbols"] == 2
    assert snap.metadata["repository_kb"]["counts"]["summaries"] == 3
    core_file = [
        record
        for record in kb["files"]
        if record.metadata["file_path"] == "comfy_extras/nodes_train.py"
    ][0]
    assert "changed" not in core_file.metadata
    assert "changed" not in core_file.tags
    assert kb["review_overlay"]["changed_files"] == ["comfy_extras/nodes_train.py"]

    out = query_review_kb(
        state={
            "run_id": "kb-test",
            "repo_path": str(tmp_path),
            "git_diff": "",
            "snapshot_root": root,
        },
        query="LoRA tensor shape",
        path="comfy_extras/nodes_train.py",
        topics=["lora", "tensor-shape"],
    )
    assert out["skipped"] is False
    assert "tensor shape" in out["answer"].lower()

    repo_out = query_repository_kb(
        state={
            "run_id": "kb-test",
            "repo_path": str(tmp_path),
            "git_diff": "",
            "snapshot_root": root,
        },
        query="LoRA tensor shape",
        path="comfy_extras/nodes_train.py",
        topics=["lora", "tensor-shape"],
    )
    assert repo_out["skipped"] is False
    assert repo_out["result"]["diagnostics"]["use_review_overlay"] is False

    overlay_out = query_review_kb(
        state={
            "run_id": "kb-test",
            "repo_path": str(tmp_path),
            "git_diff": "",
            "snapshot_root": root,
        },
        query="changed review anchors",
        max_results=4,
    )
    assert overlay_out["skipped"] is False
    assert "comfy_extras/nodes_train.py" in overlay_out["answer"]
    assert overlay_out["result"]["diagnostics"]["use_review_overlay"] is True


def test_snapshot_writer_persists_distilled_repository_kb_summaries(tmp_path: Path) -> None:
    payload, topo = _fixture_graph(tmp_path)
    settings = Settings(snapshot_base_path=tmp_path / "snapshots")
    writer = SnapshotWriter(settings)
    bundle = build_review_kb(
        run_id="kb-test",
        repo_path=str(tmp_path),
        graph_payload=payload,
        topology=topo,
    )
    fallback = [r for r in bundle.summaries if r.id == "summary:community:1"][0]
    distilled = fallback.model_copy(
        update={
            "summary": "LLM distilled LoRA adapter contracts.",
            "confidence": "llm_synthesized",
            "metadata": {
                **fallback.metadata,
                "source_record_ids": [fallback.id],
                "llm_distillation_status": "ok",
            },
        }
    )
    shard = fallback.model_copy(
        update={
            "id": "summary:community:1:shard:contracts_facts:0",
            "summary": "Shard contract summary.",
            "metadata": {
                **fallback.metadata,
                "summary_scope": "community_shard",
                "shard_id": "contracts_facts:0",
                "lane": "contracts_facts",
                "source_record_ids": [fallback.id],
                "llm_distillation_status": "ok",
            },
        }
    )

    _snap, root = writer.write_snapshot(
        run_id="kb-test",
        repo_path=str(tmp_path),
        enriched_graph_payload=payload,
        topology=topo,
        community_summaries=[],
        global_summary="summary",
        diagnostics=SnapshotDiagnostics(),
        unresolved_calls=[],
        extraction_gap_count=0,
        repository_kb_summary_records=[distilled.model_dump(mode="json"), shard.model_dump(mode="json")],
    )

    kb = load_review_kb(root)
    record = [r for r in kb["summaries"] if r.id == "summary:community:1"][0]
    assert record.confidence == "llm_synthesized"
    assert "LLM distilled" in record.summary
    assert any(r.id == "summary:community:1:shard:contracts_facts:0" for r in kb["summaries"])
