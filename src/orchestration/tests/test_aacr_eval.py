from __future__ import annotations

import csv
import json
from pathlib import Path

from src.reviewer_agent.harness import aacr_eval


def _finding(**overrides: object) -> dict:
    data = {
        "id": "logic-1:candidate",
        "file_path": "comfy_extras/nodes_string.py",
        "line_start": 175,
        "line_end": 189,
        "content": "StringCompare.execute lacks a fallback return for unknown mode values.",
        "counterexample": "mode='Unknown' reaches the end of execute without returning.",
    }
    data.update(overrides)
    return data


def _reference(**overrides: object) -> dict:
    data = {
        "note": "`execute` does not return a value for unexpected `mode` values.",
        "path": "comfy_extras/nodes_string.py",
        "side": "right",
        "from_line": 189,
        "to_line": 194,
    }
    data.update(overrides)
    return data


def _record() -> dict:
    return {
        "githubPrUrl": "https://github.com/comfyanonymous/ComfyUI/pull/7952",
        "source_commit": "924d771e18000f4cb223575189daa6d2c6c5a9c1",
        "target_commit": "4936d01872b8719ede33f9a11dc8e898c33d39e4",
        "comments": [
            {"note": "unused helper", "path": "comfy_extras/nodes_string.py", "from_line": 5, "to_line": 5, "side": "right"},
            {"note": "missing return", "path": "comfy_extras/nodes_string.py", "from_line": 194, "to_line": 189, "side": "right"},
            {"note": "", "path": "comfy_extras/nodes_string.py", "from_line": 1, "to_line": 1, "side": "right"},
            {"note": "no path", "path": "", "from_line": 1, "to_line": 1},
        ],
    }


def test_diff_location_is_same_matches_upstream_rule() -> None:
    assert aacr_eval.diff_location_is_same(189, 175, 194, 189, k=1) is True  # overlap
    assert aacr_eval.diff_location_is_same(189, 175, 194, 188, k=1) is True  # distance 1
    assert aacr_eval.diff_location_is_same(189, 175, 194, 187, k=1) is False  # distance 2
    assert aacr_eval.diff_location_is_same(189, 175, 194, 187, k=2) is True


def test_match_references_is_one_to_one_in_reference_order() -> None:
    references = [
        _reference(from_line=5, to_line=5),
        _reference(from_line=189, to_line=194),
        _reference(from_line=275, to_line=283),
        _reference(from_line=285, to_line=290),
    ]
    generated = aacr_eval.generated_comments_from_findings(
        [
            _finding(line_start=175, line_end=188),
            _finding(id="wide", line_start=190, line_end=323),
        ]
    )

    result = aacr_eval.match_references(references, generated, k=1)

    line_matches = [ref["line_match"] for ref in result["references"]]
    assert line_matches == [False, True, True, False]
    assert result["references"][1]["line_finding_index"] == 0
    assert result["references"][2]["line_finding_index"] == 1
    assert result["generated"][1]["line_matched_reference_index"] == 2
    assert result["semantic_status"] == "not_run"
    stats = aacr_eval.compute_statistics(result["references"], 2)
    assert stats["line_match_count"] == 2
    assert stats["line_match_rate"] == 1.0
    assert stats["line_recall_rate"] == 0.5
    assert stats["line_unmatched_count"] == 0


def test_match_references_path_side_and_missing_line_rules() -> None:
    references = [
        _reference(path="other.py"),
        _reference(side="left"),
        _reference(side=None, from_line=None, to_line=None),
    ]
    generated = aacr_eval.generated_comments_from_findings([_finding()])

    result = aacr_eval.match_references(references, generated, k=1)

    assert [ref["line_match"] for ref in result["references"]] == [False, False, True]


def test_match_references_skips_empty_notes_and_uses_judge_once_per_generated() -> None:
    references = [_reference(), _reference(from_line=176, to_line=176, note="same issue again")]
    generated = aacr_eval.generated_comments_from_findings([_finding(content="", counterexample=""), _finding()])
    calls: list[tuple[str, str]] = []

    def judge(reference_note: str, generated_note: str) -> bool:
        calls.append((reference_note, generated_note))
        return True

    result = aacr_eval.match_references(references, generated, k=1, judge=judge)

    assert result["semantic_status"] == "judged"
    assert result["references"][0]["semantic_match"] is True
    assert result["references"][0]["semantic_finding_index"] == 1
    assert result["references"][1]["line_match"] is False  # finding 1 already consumed by line
    assert result["references"][1]["semantic_match"] is False  # and by semantic
    assert len(calls) == 1


def test_parse_judge_answer_matches_upstream_parsing() -> None:
    assert aacr_eval.parse_judge_answer("Yes, they express the same concern.") is True
    assert aacr_eval.parse_judge_answer("No.") is False
    assert aacr_eval.parse_judge_answer("no, not the same; yes they differ") is False
    assert aacr_eval.parse_judge_answer("These are equivalent") is True


def test_official_instance_uses_target_commit_and_converter_filters() -> None:
    instance = aacr_eval.official_instance(_record())

    assert instance is not None
    assert instance["instance_id"] == "comfyanonymous__ComfyUI@4936d01"
    assert instance["base_commit"].startswith("924d771")
    assert instance["head_commit"].startswith("4936d01")
    assert [c["start_line"] for c in instance["reference_comments"]] == [5, 189]
    assert instance["reference_comments"][1]["end_line"] == 194
    assert aacr_eval.safe_id("a/b@1234567") == "a__b@1234567"
    assert aacr_eval.official_instance({"githubPrUrl": "https://example.com/x"}) is None


def test_official_result_payload_has_ocr_shape() -> None:
    payload = aacr_eval.official_result_payload(
        [_finding(), _finding(content="", counterexample="")],
        duration_seconds=12.5,
        input_tokens=100,
        output_tokens=20,
        run_id="run1",
        pr_url="https://github.com/comfyanonymous/ComfyUI/pull/7952",
    )

    assert list(payload["review"].keys()) == ["comments", "summary"]
    assert payload["review"]["comments"] == [
        {
            "path": "comfy_extras/nodes_string.py",
            "start_line": 175,
            "end_line": 189,
            "content": (
                "StringCompare.execute lacks a fallback return for unknown mode values.\n"
                "mode='Unknown' reaches the end of execute without returning."
            ),
        }
    ]
    assert payload["review"]["summary"] == {"input_tokens": 100, "output_tokens": 20}
    assert payload["duration_seconds"] == 12.5


def test_reference_loading_and_dataset_pin(tmp_path: Path) -> None:
    path = tmp_path / "positive_samples.json"
    path.write_text(json.dumps([_record()]), encoding="utf-8")

    references = aacr_eval.references_by_pr(aacr_eval.load_reference_records(path))
    key = aacr_eval.canonical_pr_key("https://github.com/ComfyAnonymous/ComfyUI/pull/7952/")
    pin = aacr_eval.dataset_pin(path)

    assert key in references
    assert [c["from_line"] for c in references[key]["comments"]] == [5, 189]
    assert pin["exists"] is True
    assert pin["sha256"] == aacr_eval.sha256_of_file(path)
    assert pin["matches_upstream_meta"] is False
    assert pin["pr_count"] == 1
    assert pin["comment_count"] == 2
    assert aacr_eval.dataset_pin(tmp_path / "missing.json")["exists"] is False


def test_load_negative_comments_reads_label_zero_rows(tmp_path: Path) -> None:
    csv_path = tmp_path / "raw.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["pr_url", "path", "side", "from_line", "to_line", "note", "category", "is_ai_comment", "label"])
        writer.writeheader()
        writer.writerow({"pr_url": "https://github.com/o/r/pull/1", "path": "a.py", "side": "right", "from_line": 71, "to_line": 71, "note": "wrong combo syntax", "category": "Code Defect", "is_ai_comment": "False", "label": "0"})
        writer.writerow({"pr_url": "https://github.com/o/r/pull/1", "path": "a.py", "side": "right", "from_line": 10, "to_line": 12, "note": "real issue", "category": "Code Defect", "is_ai_comment": "True", "label": "1"})

    negatives = aacr_eval.load_negative_comments(csv_path)

    assert list(negatives) == ["o/r#1"]
    assert negatives["o/r#1"][0]["from_line"] == 71
    result = aacr_eval.evaluate_pr(
        pr_url="https://github.com/o/r/pull/1",
        findings=[_finding(file_path="a.py", line_start=70, line_end=72)],
        references=[],
        negatives=negatives["o/r#1"],
    )
    assert result["negative_line_match_count"] == 1
    assert result["negative_line_matches"][0]["line_finding_index"] == 0


def _raw_with_pipeline(*, decision: str, gate_decision: str | None, adjudicator_decision: str | None) -> dict:
    check = {
        "check_id": "logic-1:check:1",
        "patch_task_id": "logic-1",
        "file_path": "comfy_extras/nodes_string.py",
        "line_start": 176,
        "line_end": 192,
    }
    candidate = {
        "candidate_id": "logic-1:cand",
        "file_path": "comfy_extras/nodes_string.py",
        "line_start": 184,
        "line_end": 191,
    }
    result = {
        "check_id": check["check_id"],
        "decision": decision,
        "contract_status": "contradicted" if decision == "candidate" else "missing",
        "candidate": candidate if decision == "candidate" else None,
    }
    gate_lifecycle = (
        {candidate["candidate_id"]: {"decision": gate_decision, "check_id": check["check_id"], "reason": "evidence_gate_passed" if gate_decision == "passed" else "weak_contract_proof"}}
        if gate_decision
        else {}
    )
    adj_lifecycle = {candidate["candidate_id"]: {"decision": adjudicator_decision, "reason": "x"}} if adjudicator_decision else {}
    return {
        "metadata": {
            "review_planner": {"tasks": [{"id": "logic-1", "target_files": ["comfy_extras/nodes_string.py"]}]},
            "review_checks": {
                "by_task": {
                    "logic-1": {
                        "compiled_checks": [check],
                        "gate": {"candidate_lifecycle": gate_lifecycle},
                    }
                }
            },
            "review_adjudicator": {"candidate_lifecycle": adj_lifecycle},
        },
        "review_checks": [check],
        "invalid_review_checks": [],
        "review_check_results": [result],
        "candidate_findings": [candidate] if gate_decision == "passed" else [],
    }


def test_attribute_reference_names_the_losing_stage() -> None:
    ref = _reference()

    assert aacr_eval.attribute_reference(ref, {"metadata": {}}, k=1)["lost_at"] == "no_task"

    no_check = {"metadata": {"review_planner": {"tasks": [{"id": "t", "target_files": ["comfy_extras/nodes_string.py"]}]}}}
    assert aacr_eval.attribute_reference(ref, no_check, k=1)["lost_at"] == "no_check"

    invalid = _raw_with_pipeline(decision="unsupported", gate_decision=None, adjudicator_decision=None)
    invalid["review_checks"] = []
    invalid["invalid_review_checks"] = [{"check": invalid["metadata"]["review_checks"]["by_task"]["logic-1"]["compiled_checks"][0], "reasons": ["file_not_in_task_targets"]}]
    assert aacr_eval.attribute_reference(ref, invalid, k=1)["lost_at"] == "check_invalid:file_not_in_task_targets"

    unsupported = _raw_with_pipeline(decision="unsupported", gate_decision=None, adjudicator_decision=None)
    assert aacr_eval.attribute_reference(ref, unsupported, k=1)["lost_at"] == "executor:unsupported"
    assert aacr_eval.attribute_reference(ref, unsupported, k=1)["executor_contract_status"] == {"logic-1:check:1": "missing"}

    gated = _raw_with_pipeline(decision="candidate", gate_decision="dropped", adjudicator_decision=None)
    assert aacr_eval.attribute_reference(ref, gated, k=1)["lost_at"] == "gate_dropped:weak_contract_proof"

    dropped = _raw_with_pipeline(decision="candidate", gate_decision="passed", adjudicator_decision="dropped")
    assert aacr_eval.attribute_reference(ref, dropped, k=1)["lost_at"] == "adjudicator_dropped"

    promoted = _raw_with_pipeline(decision="candidate", gate_decision="passed", adjudicator_decision="promoted")
    drifted = aacr_eval.attribute_reference(ref, promoted, final_findings=[_finding(line_start=20, line_end=25)], k=1)
    assert drifted["lost_at"] == "final_line_mismatch"
    consumed = aacr_eval.attribute_reference(ref, promoted, final_findings=[_finding()], k=1)
    assert consumed["lost_at"] == "final_consumed"
    matched = aacr_eval.attribute_reference({**ref, "line_match": True}, promoted, final_findings=[_finding()], k=1)
    assert matched["lost_at"] == "matched"


def test_evaluate_run_scores_manifest_and_exports_official_layout(tmp_path: Path) -> None:
    run_dir = tmp_path / "run1"
    (run_dir / "raw").mkdir(parents=True)
    (run_dir / "findings").mkdir()
    raw = _raw_with_pipeline(decision="candidate", gate_decision="passed", adjudicator_decision="promoted")
    raw["llm_trace"] = [
        {"event": "llm_response", "token_usage": {"prompt_tokens": 90, "completion_tokens": 10}},
        {"event": "llm_error", "error_type": "LengthFinishReasonError"},
    ]
    (run_dir / "raw" / "pr.json").write_text(json.dumps(raw), encoding="utf-8")
    (run_dir / "findings" / "pr.json").write_text(json.dumps([_finding()]), encoding="utf-8")
    (run_dir / "run_meta.json").write_text(json.dumps({"run_id": "run1"}), encoding="utf-8")
    with (run_dir / "manifest.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["pr_url", "slug", "status", "raw_path", "findings_path", "elapsed_ms"])
        writer.writeheader()
        writer.writerow({"pr_url": "https://github.com/comfyanonymous/ComfyUI/pull/7952", "slug": "pr", "status": "ok", "raw_path": "raw/pr.json", "findings_path": "findings/pr.json", "elapsed_ms": 4500})
        writer.writerow({"pr_url": "https://github.com/o/r/pull/9", "slug": "bad", "status": "error", "raw_path": "", "findings_path": "", "elapsed_ms": 0})
    references_path = tmp_path / "positive_samples.json"
    references_path.write_text(json.dumps([_record()]), encoding="utf-8")

    payload = aacr_eval.evaluate_run(run_dir, references_path=references_path, k=1)

    assert payload["summary"]["expected_notes"] == 2
    assert payload["summary"]["generated_notes"] == 1
    assert payload["summary"]["matched_line_notes"] == 1
    assert payload["summary"]["line_match_rate"] == 1.0
    assert payload["summary"]["line_recall_rate"] == 0.5
    assert payload["summary"]["line_f1"] == 0.667
    assert payload["summary"]["semantic_status"] == "not_run"
    assert payload["summary"]["lost_at_counts"] == {"matched": 1, "no_check": 1}
    assert payload["dataset_pin"]["pr_count"] == 1
    assert (run_dir / "evaluation.json").is_file()
    exported = json.loads((run_dir / "official" / "results" / "comfyanonymous__ComfyUI@4936d01.json").read_text(encoding="utf-8"))
    assert exported["review"]["comments"][0]["start_line"] == 175
    assert exported["review"]["summary"] == {"input_tokens": 90, "output_tokens": 10}
    assert exported["duration_seconds"] == 4.5
    slice_lines = (run_dir / "official" / "aacr_bench_slice.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(slice_lines[0])["instance_id"] == "comfyanonymous__ComfyUI@4936d01"
    assert "--reviewer ocr" in (run_dir / "official" / "README.md").read_text(encoding="utf-8")
    assert "findings" not in payload["prs"][0]


def test_judge_from_env_requires_real_credentials() -> None:
    assert aacr_eval.judge_from_env({}) is None
    assert aacr_eval.judge_from_env({"JUDGE_USE_MOCK": "true", "JUDGE_API_KEY": "k", "JUDGE_BASE_URL": "u"}) is None


def test_evaluate_run_reads_code_version_from_raw_metadata(tmp_path: Path) -> None:
    run_dir = tmp_path / "run2"
    (run_dir / "raw").mkdir(parents=True)
    (run_dir / "findings").mkdir()
    raw = _raw_with_pipeline(decision="candidate", gate_decision="passed", adjudicator_decision="promoted")
    raw["metadata"]["review_code_version"] = "annotated_head"
    (run_dir / "raw" / "pr.json").write_text(json.dumps(raw), encoding="utf-8")
    (run_dir / "findings" / "pr.json").write_text(json.dumps([_finding()]), encoding="utf-8")
    with (run_dir / "manifest.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["pr_url", "slug", "status", "raw_path", "findings_path", "elapsed_ms"])
        writer.writeheader()
        writer.writerow({"pr_url": "https://github.com/comfyanonymous/ComfyUI/pull/7952", "slug": "pr", "status": "ok", "raw_path": "raw/pr.json", "findings_path": "findings/pr.json", "elapsed_ms": 10})
    references_path = tmp_path / "positive_samples.json"
    references_path.write_text(json.dumps([_record()]), encoding="utf-8")

    payload = aacr_eval.evaluate_run(run_dir, references_path=references_path, k=1, export=False)

    assert payload["prs"][0]["code_version"] == "annotated_head"
    assert payload["summary"]["code_version_counts"] == {"annotated_head": 1}
