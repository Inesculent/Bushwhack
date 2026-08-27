from __future__ import annotations

import csv
import json
from pathlib import Path

from src.config import Settings
from src.data.research_pipeline.github_api import PullRequestContext
from src.domain.schemas import ReviewFinding
from src.reviewer_agent.harness import aacr, aacr_eval

PR_URL = "https://github.com/comfyanonymous/ComfyUI/pull/7952"
PATH = "comfy_extras/nodes_string.py"


PINNED_DIFF = f"diff --git a/{PATH} b/{PATH}\n+++ b/{PATH}\n@@\n+def normalize_path():\n+    pass\n+class StringCompare:\n"


class _StubEnricher:
    compare_diff: str | None = PINNED_DIFF
    compare_calls: list[tuple[str, str, str]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def fetch_compare_diff(self, repo: str, base_commit: str, head_commit: str) -> str | None:
        type(self).compare_calls.append((repo, base_commit, head_commit))
        return type(self).compare_diff

    def fetch_pr_context(self, pr_url: str) -> PullRequestContext:
        return PullRequestContext(
            pr_url=pr_url,
            repo="comfyanonymous/ComfyUI",
            number=7952,
            title="Add nodes for basic string operations",
            body="",
            unified_diff=f"diff --git a/{PATH} b/{PATH}\n+++ b/{PATH}\n@@\n+class StringCompare:\n",
        )


def _fake_result() -> dict:
    finding = ReviewFinding(
        id="logic-1:cand",
        file_path=PATH,
        line_start=175,
        line_end=189,
        content="StringCompare.execute lacks a fallback return for unknown mode values.",
        counterexample="mode='Unknown' reaches the end of execute without returning.",
    )
    check = {
        "check_id": "logic-1:check:1",
        "patch_task_id": "logic-1",
        "file_path": PATH,
        "line_start": 176,
        "line_end": 192,
    }
    candidate = {"candidate_id": "logic-1:cand", "file_path": PATH, "line_start": 184, "line_end": 191}
    return {
        "final_findings": [finding],
        "token_usage": 1234,
        "node_history": ["review_check_executor"],
        "metadata": {
            "review_planner": {"tasks": [{"id": "logic-1", "target_files": [PATH]}]},
            "review_checks": {
                "by_task": {
                    "logic-1": {
                        "compiled_checks": [check],
                        "gate": {
                            "promoted_count": 1,
                            "dropped_count": 0,
                            "candidate_lifecycle": {
                                "logic-1:cand": {
                                    "decision": "passed",
                                    "check_id": "logic-1:check:1",
                                    "reason": "evidence_gate_passed",
                                }
                            },
                        },
                    }
                }
            },
            "review_adjudicator": {"candidate_lifecycle": {"logic-1:cand": {"decision": "promoted", "reason": "ok"}}},
        },
        "review_checks": [check],
        "invalid_review_checks": [],
        "review_check_results": [
            {"check_id": "logic-1:check:1", "decision": "candidate", "candidate": candidate}
        ],
        "candidate_findings": [candidate],
        "focused_context_requests": [],
        "focused_context_results": {},
        "llm_trace": [
            {"event": "llm_response", "token_usage": {"prompt_tokens": 1000, "completion_tokens": 234}},
            {
                "event": "llm_error",
                "error_type": "LengthFinishReasonError",
                "elapsed_ms": 5,
                "error": "CompletionUsage(completion_tokens=1, prompt_tokens=2, total_tokens=3)",
            },
        ],
    }


def test_run_aacr_reviewer_scores_findings_and_exports_official_layout(tmp_path: Path, monkeypatch) -> None:
    references = tmp_path / "positive_samples.json"
    references.write_text(
        json.dumps(
            [
                {
                    "githubPrUrl": PR_URL,
                    "source_commit": "924d771e18000f4cb223575189daa6d2c6c5a9c1",
                    "target_commit": "4936d01872b8719ede33f9a11dc8e898c33d39e4",
                    "comments": [
                        {"note": "unused helper", "path": PATH, "from_line": 5, "to_line": 5, "side": "right"},
                        {"note": "missing return", "path": PATH, "from_line": 189, "to_line": 194, "side": "right"},
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    dataset = tmp_path / "dataset.csv"
    with dataset.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["pr_url", "target_language"])
        writer.writeheader()
        writer.writerow({"pr_url": PR_URL, "target_language": "python"})
    settings = Settings(
        snapshot_base_path=str(tmp_path),
        reviewer_agent_output_dir=tmp_path / "out",
        github_mcp_enabled=False,
        redis_enabled=False,
    )
    monkeypatch.setattr(aacr, "get_settings", lambda: settings)
    monkeypatch.setattr(aacr, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(aacr, "DEFAULT_POSITIVE_SAMPLES_PATH", references)
    monkeypatch.setattr(aacr, "DEFAULT_NEGATIVE_SAMPLES_PATH", tmp_path / "missing.csv")
    monkeypatch.setattr(aacr, "GitHubPullRequestEnricher", _StubEnricher)
    captured: dict = {}

    def _invoke(**kwargs: object) -> dict:
        captured.update(kwargs)
        return _fake_result()

    monkeypatch.setattr(aacr, "_invoke_for_pr", _invoke)
    monkeypatch.setattr(_StubEnricher, "compare_diff", PINNED_DIFF)
    monkeypatch.setattr(_StubEnricher, "compare_calls", [])
    monkeypatch.delenv("JUDGE_API_KEY", raising=False)
    monkeypatch.delenv("JUDGE_BASE_URL", raising=False)

    artifacts = aacr.run_aacr_reviewer(dataset_path=dataset, run_id="scoringtest", pr_urls=[PR_URL])

    assert artifacts.succeeded == 1 and artifacts.failed == 0
    # The pinned (annotated) commit range is reviewed, not the live PR head.
    assert _StubEnricher.compare_calls == [
        ("comfyanonymous/ComfyUI", "924d771e18000f4cb223575189daa6d2c6c5a9c1", "4936d01872b8719ede33f9a11dc8e898c33d39e4")
    ]
    assert captured["context"].unified_diff == PINNED_DIFF
    assert captured["code_version"] == "annotated_head"
    assert captured["review_commits"]["head_commit"].startswith("4936d01")
    run_dir = artifacts.output_dir
    with artifacts.manifest_path.open("r", encoding="utf-8", newline="") as handle:
        row = list(csv.DictReader(handle))[0]
    assert row["line_match_count"] == "1"
    assert row["line_recall_rate"] == "0.5"
    assert row["semantic_status"] == "not_run"
    assert json.loads(row["reference_lost_at"]) == {"matched": 1, "no_check": 1}
    assert row["evaluation_path"] == f"evaluation/{row['slug']}.json"
    assert row["length_limit_failure_count"] == "1"
    assert row["reviewed_code_version"] == "annotated_head"

    evaluation = json.loads((run_dir / "evaluation.json").read_text(encoding="utf-8"))
    assert evaluation["summary"]["expected_notes"] == 2
    assert evaluation["summary"]["matched_line_notes"] == 1
    assert evaluation["summary"]["line_f1"] == 0.667
    assert evaluation["summary"]["lost_at_counts"] == {"matched": 1, "no_check": 1}
    assert evaluation["dataset_pin"]["sha256"] == aacr_eval.sha256_of_file(references)
    assert "findings" not in evaluation["prs"][0]
    per_pr = json.loads((run_dir / row["evaluation_path"]).read_text(encoding="utf-8"))
    assert per_pr["attribution"][1]["lost_at"] == "matched"

    run_meta = json.loads(artifacts.run_meta_path.read_text(encoding="utf-8"))
    assert run_meta["dataset_pin"]["pr_count"] == 1
    assert run_meta["dataset_pin"]["matches_upstream_meta"] is False
    assert "positive_samples_sha256_differs_from_upstream_meta" in run_meta["run_warnings"]
    assert run_meta["evaluation_summary"]["line_recall_rate"] == 0.5
    assert run_meta["evaluation_summary"]["code_version_counts"] == {"annotated_head": 1}
    assert "annotated_head_unresolvable" not in run_meta["run_warnings"]
    assert run_meta["official_export"]["written"] == ["comfyanonymous__ComfyUI@4936d01.json"]
    assert run_meta["length_limit_failures"] == {"count": 1, "tokens": 3, "elapsed_ms": 5}

    exported = json.loads(
        (run_dir / "official" / "results" / "comfyanonymous__ComfyUI@4936d01.json").read_text(encoding="utf-8")
    )
    assert exported["review"]["comments"][0]["start_line"] == 175
    assert exported["review"]["summary"] == {"input_tokens": 1000, "output_tokens": 234}
    assert exported["reviewed_code_version"] == "annotated_head"
    slice_rows = (run_dir / "official" / "aacr_bench_slice.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(slice_rows[0])["reference_comments"][1]["start_line"] == 189


def test_run_aacr_reviewer_warns_when_references_are_missing(tmp_path: Path, monkeypatch) -> None:
    dataset = tmp_path / "dataset.csv"
    with dataset.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["pr_url", "target_language"])
        writer.writeheader()
        writer.writerow({"pr_url": PR_URL, "target_language": "python"})
    settings = Settings(
        snapshot_base_path=str(tmp_path),
        reviewer_agent_output_dir=tmp_path / "out",
        github_mcp_enabled=False,
        redis_enabled=False,
    )
    monkeypatch.setattr(aacr, "get_settings", lambda: settings)
    monkeypatch.setattr(aacr, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(aacr, "DEFAULT_POSITIVE_SAMPLES_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(aacr, "DEFAULT_NEGATIVE_SAMPLES_PATH", tmp_path / "missing.csv")
    monkeypatch.setattr(aacr, "GitHubPullRequestEnricher", _StubEnricher)
    monkeypatch.setattr(aacr, "_invoke_for_pr", lambda **kwargs: _fake_result())
    monkeypatch.setattr(
        aacr_eval,
        "ensure_reference_file",
        lambda path, download=True, timeout=60: {**aacr_eval.dataset_pin(path), "error": "download_failed: offline"},
    )

    artifacts = aacr.run_aacr_reviewer(dataset_path=dataset, run_id="noreferences", pr_urls=[PR_URL])

    run_meta = json.loads(artifacts.run_meta_path.read_text(encoding="utf-8"))
    assert "positive_samples_missing" in run_meta["run_warnings"]
    assert run_meta["dataset_pin"]["exists"] is False
    assert run_meta["evaluation_summary"]["expected_notes"] == 0
    assert run_meta["evaluation_summary"]["generated_notes"] == 1
    assert run_meta["official_export"]["written"] == []
    assert run_meta["official_export"]["skipped_pr_urls"] == [PR_URL]


def test_run_aacr_reviewer_falls_back_to_live_head_when_annotated_commit_is_gone(tmp_path: Path, monkeypatch) -> None:
    references = tmp_path / "positive_samples.json"
    references.write_text(
        json.dumps(
            [
                {
                    "githubPrUrl": PR_URL,
                    "source_commit": "924d771e18000f4cb223575189daa6d2c6c5a9c1",
                    "target_commit": "4936d01872b8719ede33f9a11dc8e898c33d39e4",
                    "comments": [
                        {"note": "missing return", "path": PATH, "from_line": 189, "to_line": 194, "side": "right"},
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    dataset = tmp_path / "dataset.csv"
    with dataset.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["pr_url", "target_language"])
        writer.writeheader()
        writer.writerow({"pr_url": PR_URL, "target_language": "python"})
    settings = Settings(
        snapshot_base_path=str(tmp_path),
        reviewer_agent_output_dir=tmp_path / "out",
        github_mcp_enabled=False,
        redis_enabled=False,
    )
    monkeypatch.setattr(aacr, "get_settings", lambda: settings)
    monkeypatch.setattr(aacr, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(aacr, "DEFAULT_POSITIVE_SAMPLES_PATH", references)
    monkeypatch.setattr(aacr, "DEFAULT_NEGATIVE_SAMPLES_PATH", tmp_path / "missing.csv")
    monkeypatch.setattr(aacr, "GitHubPullRequestEnricher", _StubEnricher)
    monkeypatch.setattr(_StubEnricher, "compare_diff", None)
    monkeypatch.setattr(_StubEnricher, "compare_calls", [])
    captured: dict = {}

    def _invoke(**kwargs: object) -> dict:
        captured.update(kwargs)
        return _fake_result()

    monkeypatch.setattr(aacr, "_invoke_for_pr", _invoke)
    monkeypatch.delenv("JUDGE_API_KEY", raising=False)
    monkeypatch.delenv("JUDGE_BASE_URL", raising=False)

    artifacts = aacr.run_aacr_reviewer(dataset_path=dataset, run_id="livehead", pr_urls=[PR_URL])

    run_meta = json.loads(artifacts.run_meta_path.read_text(encoding="utf-8"))
    assert captured["code_version"] == "live_head"
    assert "review_checkout_ref" not in captured  # only the metadata built inside _invoke_for_pr carries it
    assert "annotated_head_unresolvable" in run_meta["run_warnings"]
    assert run_meta["evaluation_summary"]["code_version_counts"] == {"live_head": 1}
    with artifacts.manifest_path.open("r", encoding="utf-8", newline="") as handle:
        row = list(csv.DictReader(handle))[0]
    assert row["reviewed_code_version"] == "live_head"


def test_invoke_for_pr_metadata_pins_checkout_ref_only_for_annotated_head() -> None:
    import logging

    context = PullRequestContext(
        pr_url=PR_URL, repo="comfyanonymous/ComfyUI", number=7952, title="t", body="b", unified_diff="diff"
    )
    commits = {"base_commit": "924d771e18000f4cb223575189daa6d2c6c5a9c1", "head_commit": "4936d01872b8719ede33f9a11dc8e898c33d39e4"}
    seen: dict = {}

    def _fake_run_reviewer(state: dict) -> dict:
        seen.update(state["metadata"])
        return {"final_findings": [], "token_usage": 0, "metadata": dict(state["metadata"]), "llm_trace": []}

    aacr._invoke_for_pr(
        run_id="r",
        pr_url=PR_URL,
        context=context,
        repo_root=None,
        trace=False,
        started_at="now",
        run_reviewer_fn=_fake_run_reviewer,
        experiment_tag="t",
        logger=logging.getLogger("test"),
        review_commits=commits,
        code_version="annotated_head",
    )
    assert seen["review_checkout_ref"] == commits["head_commit"]
    assert seen["review_code_version"] == "annotated_head"
    assert seen["review_base_commit"] == commits["base_commit"]

    seen.clear()
    aacr._invoke_for_pr(
        run_id="r",
        pr_url=PR_URL,
        context=context,
        repo_root=None,
        trace=False,
        started_at="now",
        run_reviewer_fn=_fake_run_reviewer,
        experiment_tag="t",
        logger=logging.getLogger("test"),
        review_commits=commits,
        code_version="live_head",
    )
    assert "review_checkout_ref" not in seen
    assert seen["review_code_version"] == "live_head"


def test_fetch_compare_diff_uses_compare_api_with_diff_accept(monkeypatch) -> None:
    import logging

    from src.data.research_pipeline.github_api import GitHubPullRequestEnricher

    enricher = GitHubPullRequestEnricher(logger=logging.getLogger("test"))
    calls: list[tuple[str, str]] = []

    def _fake_request_text(api_url: str, context: str, accept: str) -> str | None:
        calls.append((api_url, accept))
        return "diff --git a/x b/x"

    monkeypatch.setattr(enricher, "_request_text", _fake_request_text)

    assert enricher.fetch_compare_diff("o/r", "aaaa111", "bbbb222") == "diff --git a/x b/x"
    assert calls == [("https://api.github.com/repos/o/r/compare/aaaa111...bbbb222", "application/vnd.github.v3.diff")]
    assert enricher.fetch_compare_diff("o/r", "", "bbbb222") is None
