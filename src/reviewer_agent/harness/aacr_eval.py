"""Self-scoring for AACR-Bench runs, vendored from the official evaluator.

The matching rules, statistics, instance-id rule, and result layout below are
transcribed from https://github.com/alibaba/aacr-bench at commit
``68a569759289a83654a59d06db2a72910edf0a4a`` (2026-08-24):

- ``evaluation/judge.py``: path normalization, ``diff_location_is_same``,
  the reference-ordered one-to-one pairing in ``evaluate_comments``,
  ``compute_cr_statistics``, and the semantic-judge prompt;
- ``evaluation/evaluate.py``: line-range normalization, the OCR result
  shape, and the summary/F1 formulas;
- ``evaluation/converters/aacr_bench.py``: the ``ReviewInstance`` mapping and
  the ``instance_id`` rule (``owner__name@<target_commit[:7]>``).

They are vendored rather than imported because the upstream package uses
top-level module names (``config``, ``schema``, ``judge``) that collide with
this repository.

Everything here consumes finished run artifacts. Reference comments never
reach the reviewer graph; scoring happens after ``final_findings`` exist.

Two local additions go beyond the official evaluator and are reported
separately so they cannot be confused with official numbers: per-reference
stage attribution (where in the pipeline each reference was lost) and line
matches against negative (label 0) comments from the annotated dataset.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

UPSTREAM_REPO = "https://github.com/alibaba/aacr-bench"
UPSTREAM_COMMIT = "68a569759289a83654a59d06db2a72910edf0a4a"
UPSTREAM_META = {
    "filename": "positive_samples.json",
    "url": "https://raw.githubusercontent.com/alibaba/aacr-bench/main/dataset/positive_samples.json",
    "sha256": "d8683cb240249bc4e0aff6428802bdffa7b7573ace600552cab1cd0cb7e905c9",
}
HF_DATASET = "Alibaba-Aone/aacr-bench"
HF_DATASET_REVISION = "47be1d6df1e7faf222cf531587772d92f79fe6b2"
DEFAULT_LINE_K = 1
EVALUATION_SCHEMA_VERSION = 1

JUDGE_BASE_URL_VAR = "JUDGE_BASE_URL"
JUDGE_API_KEY_VAR = "JUDGE_API_KEY"
JUDGE_MODEL_VAR = "JUDGE_MODEL"
JUDGE_USE_MOCK_VAR = "JUDGE_USE_MOCK"

# Verbatim from evaluation/judge.py::match_semantic.
SEMANTIC_JUDGE_PROMPT = (
    "-Role-\n"
    "You are an expert code reviewer assistant specialized in analyzing and "
    "comparing code review comments.\n\n"
    "-Task-\n"
    "Determine whether two given review comments express the same concern or "
    "suggestion. Ignore differences in wording, tone, or formatting—focus solely "
    "on semantic equivalence of the underlying issue. If the core intent and "
    'technical substance are identical, answer "yes"; otherwise, answer "no".\n\n'
    "Review Comment 1:\n{reference_note}\n\n"
    "Review Comment 2:\n{generated_note}\n\n"
    "-Task-\n"
    "Determine whether the two review comments given above express the same concern or "
    "suggestion. Ignore differences in wording, tone, or formatting—focus "
    "solely on semantic equivalence of the underlying issue. If the core intent and "
    'technical substance are identical, answer "yes"; otherwise, answer "no".\n\n'
    "Your answer:"
)

_GITHUB_PR_PATTERN = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", re.IGNORECASE)

JudgeFn = Callable[[str, str], bool]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, float) and value != value:  # NaN
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_path(path: str) -> str:
    """Vendored ``judge._normalize_path``."""
    if not path:
        return ""
    return path.replace("\\/", "/").replace("\\", "/")


def strip_thinking_tags(text: str) -> str:
    """Vendored ``judge._strip_thinking_tags``."""
    if not text:
        return ""
    cleaned = re.sub(r"<details>.*?</details>", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def diff_location_is_same(
    from_line1: int, from_line2: int, to_line1: int, to_line2: int, k: int = DEFAULT_LINE_K
) -> bool:
    """Vendored ``judge.diff_location_is_same``: overlap, or minimum distance within k."""
    has_overlap = not (from_line1 > to_line2 or from_line2 < to_line1)
    if has_overlap:
        min_distance = 0
    else:
        min_distance = min(abs(from_line1 - to_line2), abs(from_line2 - to_line1))
    return min_distance <= k


def normalize_line_range(
    from_line: Optional[int], to_line: Optional[int]
) -> tuple[Optional[int], Optional[int]]:
    """Vendored ``evaluate._normalize_line_range``: fill the missing bound, order the pair."""
    if from_line is None and to_line is None:
        return None, None
    if from_line is None:
        from_line = to_line
    if to_line is None:
        to_line = from_line
    if from_line > to_line:
        from_line, to_line = to_line, from_line
    return from_line, to_line


def parse_repo_from_pr_url(pr_url: Any) -> Optional[str]:
    """Vendored converter rule: ``owner/name`` from a GitHub PR URL, case preserved."""
    if not isinstance(pr_url, str):
        return None
    match = _GITHUB_PR_PATTERN.search(pr_url)
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def canonical_pr_key(pr_url: str) -> str:
    """Case-insensitive ``owner/name#number`` key used to join runs with references."""
    match = _GITHUB_PR_PATTERN.search(pr_url or "")
    if not match:
        return (pr_url or "").strip().rstrip("/").lower()
    return f"{match.group(1)}/{match.group(2)}#{match.group(3)}".lower()


def safe_id(instance_id: str) -> str:
    return instance_id.replace("/", "__")


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Reference data (official positive_samples.json)
# ---------------------------------------------------------------------------


def load_reference_records(path: Path) -> List[Dict[str, Any]]:
    """Load the official ``positive_samples.json`` (a JSON array of PR records)."""
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError(f"{path}: expected a JSON array of PR records, got {type(rows).__name__}")
    return [row for row in rows if isinstance(row, dict)]


def reference_comments_for_record(record: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Reference comments in the comparison shape the official judge consumes.

    Applies the converter's filter (non-empty ``path`` and ``note``) and the
    evaluator's line-range normalization. Annotation fields are carried along
    for reporting only.
    """
    comments: List[Dict[str, Any]] = []
    for raw in record.get("comments") or []:
        if not isinstance(raw, Mapping):
            continue
        path = raw.get("path")
        text = raw.get("note")
        if not path or not text or not str(text).strip():
            continue
        from_line, to_line = normalize_line_range(
            _int_or_none(raw.get("from_line")), _int_or_none(raw.get("to_line"))
        )
        side = raw.get("side")
        side = str(side).strip() if side is not None else None
        comments.append(
            {
                "note": str(text).strip(),
                "path": str(path).strip(),
                "side": side,
                "from_line": from_line,
                "to_line": to_line,
                "category": raw.get("category"),
                "context": raw.get("context"),
                "is_ai_comment": raw.get("is_ai_comment"),
                "source_model": raw.get("source_model"),
            }
        )
    return comments


def official_instance(record: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Vendored ``converters.aacr_bench.convert_record`` as a plain dict.

    ``source_commit`` is the base and ``target_commit`` the head; the instance
    id is ``owner__name@<target_commit[:7]>``. The official evaluator looks up
    result files by this id, so the rule must not be re-derived elsewhere.
    """
    repo = parse_repo_from_pr_url(record.get("githubPrUrl"))
    base_commit = record.get("source_commit")
    head_commit = record.get("target_commit")
    if not repo or not base_commit or not head_commit:
        return None
    instance_id = f"{repo.replace('/', '__')}@{str(head_commit)[:7]}"
    reference_comments = []
    for comment in reference_comments_for_record(record):
        item = {
            "path": comment["path"],
            "start_line": comment["from_line"],
            "end_line": comment["to_line"],
            "text": comment["note"],
        }
        if comment["side"] is not None:
            item["side"] = comment["side"]
        reference_comments.append(item)
    return {
        "instance_id": instance_id,
        "repo": repo,
        "base_commit": str(base_commit).strip(),
        "head_commit": str(head_commit).strip(),
        "reference_comments": reference_comments,
    }


def references_by_pr(records: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for record in records:
        pr_url = str(record.get("githubPrUrl") or record.get("pr_url") or "").strip()
        if not pr_url:
            continue
        out[canonical_pr_key(pr_url)] = {
            "pr_url": pr_url,
            "record": dict(record),
            "instance": official_instance(record),
            "comments": reference_comments_for_record(record),
        }
    return out


def dataset_pin(path: Path) -> Dict[str, Any]:
    """Describe exactly which reference file a run scored against."""
    path = Path(path)
    pin: Dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "sha256": None,
        "pr_count": 0,
        "comment_count": 0,
        "upstream_repo": UPSTREAM_REPO,
        "upstream_commit": UPSTREAM_COMMIT,
        "upstream_url": UPSTREAM_META["url"],
        "upstream_meta_sha256": UPSTREAM_META["sha256"],
        "matches_upstream_meta": None,
        "hf_dataset": HF_DATASET,
        "hf_dataset_revision": HF_DATASET_REVISION,
    }
    if not path.exists():
        return pin
    pin["sha256"] = sha256_of_file(path)
    pin["matches_upstream_meta"] = pin["sha256"] == UPSTREAM_META["sha256"]
    try:
        records = load_reference_records(path)
    except Exception as exc:  # noqa: BLE001
        pin["error"] = f"{exc.__class__.__name__}: {exc}"
        return pin
    pin["pr_count"] = len(records)
    pin["comment_count"] = sum(len(reference_comments_for_record(record)) for record in records)
    return pin


def ensure_reference_file(path: Path, *, download: bool = True, timeout: int = 60) -> Dict[str, Any]:
    """Return the dataset pin, fetching the official file first when it is absent.

    Never raises: a missing or unreachable file is reported in the pin so the
    run can record it as a warning instead of silently scoring against nothing.
    """
    path = Path(path)
    if not path.exists() and download:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_name(f"{path.name}.part")
            with urllib.request.urlopen(UPSTREAM_META["url"], timeout=timeout) as response:
                with tmp_path.open("wb") as handle:
                    for chunk in iter(lambda: response.read(1 << 16), b""):
                        handle.write(chunk)
            os.replace(tmp_path, path)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            pin = dataset_pin(path)
            pin["error"] = f"download_failed: {exc.__class__.__name__}: {exc}"
            return pin
    return dataset_pin(path)


# ---------------------------------------------------------------------------
# Generated comments and the official result layout
# ---------------------------------------------------------------------------


def generated_comments_from_findings(findings: Iterable[Any]) -> List[Dict[str, Any]]:
    """Findings in the comparison shape, with the finding index for traceability.

    The judged text mirrors the official Claude adapter (summary + failure
    scenario): finding ``content`` plus its ``counterexample`` when present.
    The reviewer comments on the new side of the diff, so ``side`` is
    ``"right"`` exactly as the official adapters hard-code it.
    """
    comments: List[Dict[str, Any]] = []
    for index, finding in enumerate(findings):
        content = str(_field(finding, "content", "") or "").strip()
        counterexample = str(_field(finding, "counterexample", "") or "").strip()
        note = "\n".join(part for part in [content, counterexample] if part).strip()
        from_line, to_line = normalize_line_range(
            _int_or_none(_field(finding, "line_start")), _int_or_none(_field(finding, "line_end"))
        )
        comments.append(
            {
                "note": note,
                "path": normalize_path(str(_field(finding, "file_path", "") or "")),
                "side": "right",
                "from_line": from_line,
                "to_line": to_line,
                "finding_index": index,
                "finding_id": str(_field(finding, "id", "") or ""),
                "span_width": (to_line - from_line + 1) if from_line is not None and to_line is not None else None,
            }
        )
    return comments


def official_result_payload(
    findings: Iterable[Any],
    *,
    duration_seconds: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    run_id: str = "",
    pr_url: str = "",
) -> Dict[str, Any]:
    """The OCR-style result file the official ``--reviewer ocr`` evaluation reads."""
    comments = []
    for generated in generated_comments_from_findings(findings):
        if not generated["note"]:
            continue
        comments.append(
            {
                "path": generated["path"],
                "start_line": generated["from_line"],
                "end_line": generated["to_line"],
                "content": generated["note"],
            }
        )
    return {
        "review": {
            "comments": comments,
            "summary": {"input_tokens": int(input_tokens), "output_tokens": int(output_tokens)},
        },
        "duration_seconds": float(duration_seconds),
        "review_output_source": "bushwhack",
        "run_id": run_id,
        "pr_url": pr_url,
    }


# ---------------------------------------------------------------------------
# Matching (vendored evaluate_comments) and statistics
# ---------------------------------------------------------------------------


def match_references(
    references: Sequence[Mapping[str, Any]],
    generated: Sequence[Mapping[str, Any]],
    *,
    k: int = DEFAULT_LINE_K,
    judge: Optional[JudgeFn] = None,
) -> Dict[str, Any]:
    """Vendored ``judge.evaluate_comments`` on copies of the inputs.

    References are visited in order; generated comments in order. A generated
    comment can contribute one line match and one semantic match in total.
    When ``judge`` is ``None`` the semantic stage is skipped; line results are
    unaffected because they are fixed before the judge is consulted.
    """
    refs = [dict(item) for item in references]
    gens = [dict(item) for item in generated]
    matched_by_line: set[int] = set()
    matched_by_semantic: set[int] = set()

    for ref_index, ref in enumerate(refs):
        ref["line_match"] = False
        ref["semantic_match"] = False
        ref["matched_note"] = ""
        ref["line_finding_index"] = None
        ref["semantic_finding_index"] = None
        ref_note = ref.get("note", "")
        if not ref_note:
            continue
        ref_note_cleaned = strip_thinking_tags(ref_note)
        if not ref_note_cleaned:
            continue
        ref_path = normalize_path(ref.get("path", ""))
        ref_side = ref.get("side")
        ref_from_line = ref.get("from_line")
        ref_to_line = ref.get("to_line")

        line_matched = False
        semantic_matched = False
        matched_note_text = ""
        for gen_idx, gen in enumerate(gens):
            gen_note = gen.get("note", "")
            if not gen_note:
                continue
            gen_note_cleaned = strip_thinking_tags(gen_note)
            if not gen_note_cleaned:
                continue
            gen_path = normalize_path(gen.get("path", ""))
            gen_side = gen.get("side")
            gen_from_line = gen.get("from_line")
            gen_to_line = gen.get("to_line")

            if ref_path and gen_path and ref_path != gen_path:
                continue
            if ref_side is not None and gen_side is not None and ref_side != gen_side:
                continue
            if all(x is not None for x in [ref_from_line, ref_to_line, gen_from_line, gen_to_line]):
                if not diff_location_is_same(ref_from_line, ref_to_line, gen_from_line, gen_to_line, k):
                    continue

            if not line_matched and gen_idx not in matched_by_line:
                line_matched = True
                matched_by_line.add(gen_idx)
                ref["line_finding_index"] = gen.get("finding_index", gen_idx)
                gens[gen_idx]["line_matched_reference_index"] = ref_index

            if gen_idx in matched_by_semantic:
                continue
            if judge is None:
                continue
            if judge(ref_note_cleaned, gen_note_cleaned):
                semantic_matched = True
                matched_note_text = gen_note
                matched_by_semantic.add(gen_idx)
                ref["semantic_finding_index"] = gen.get("finding_index", gen_idx)
                gens[gen_idx]["semantic_matched_reference_index"] = ref_index
                break

        ref["line_match"] = line_matched
        ref["semantic_match"] = semantic_matched
        ref["matched_note"] = matched_note_text

    for gen in gens:
        gen.setdefault("line_matched_reference_index", None)
        gen.setdefault("semantic_matched_reference_index", None)
    return {
        "references": refs,
        "generated": gens,
        "semantic_status": "judged" if judge is not None else "not_run",
    }


def compute_statistics(references: Sequence[Mapping[str, Any]], generated_count: int) -> Dict[str, Any]:
    """Vendored ``judge.compute_cr_statistics`` plus the noise rate from ``docs/metrics.md``."""
    total_expected = len([c for c in references if isinstance(c, Mapping) and c.get("note")])
    line_match_count = sum(1 for c in references if isinstance(c, Mapping) and c.get("line_match", False))
    semantic_match_count = sum(
        1 for c in references if isinstance(c, Mapping) and c.get("semantic_match", False)
    )
    return {
        "expected_notes": total_expected,
        "generated_notes": generated_count,
        "line_match_count": line_match_count,
        "semantic_match_count": semantic_match_count,
        "line_match_rate": round(line_match_count / generated_count, 3) if generated_count else 0.0,
        "semantic_match_rate": round(semantic_match_count / generated_count, 3) if generated_count else 0.0,
        "line_recall_rate": round(line_match_count / total_expected, 3) if total_expected else 0.0,
        "semantic_recall_rate": round(semantic_match_count / total_expected, 3) if total_expected else 0.0,
        "line_unmatched_count": max(0, generated_count - line_match_count),
        "line_unmatched_rate": (
            round(max(0, generated_count - line_match_count) / generated_count, 3) if generated_count else 0.0
        ),
    }


def compute_f1(precision: float, recall: float) -> float:
    """Vendored ``evaluate._compute_f1``."""
    if (precision + recall) <= 0:
        return 0.0
    return round(2 * precision * recall / (precision + recall), 3)


def summarize(pr_results: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Run-level summary in the shape of ``evaluate._compute_summary``."""
    expected = sum(int(item["statistics"]["expected_notes"]) for item in pr_results)
    generated = sum(int(item["statistics"]["generated_notes"]) for item in pr_results)
    line_matched = sum(int(item["statistics"]["line_match_count"]) for item in pr_results)
    semantic_matched = sum(int(item["statistics"]["semantic_match_count"]) for item in pr_results)
    negative_matched = sum(int(item.get("negative_line_match_count") or 0) for item in pr_results)
    sem_p = round(semantic_matched / generated, 3) if generated else 0.0
    line_p = round(line_matched / generated, 3) if generated else 0.0
    sem_r = round(semantic_matched / expected, 3) if expected else 0.0
    line_r = round(line_matched / expected, 3) if expected else 0.0
    lost_at: Counter[str] = Counter()
    for item in pr_results:
        lost_at.update(item.get("lost_at_counts") or {})
    span_widths = [
        gen["span_width"]
        for item in pr_results
        for gen in item.get("generated", [])
        if gen.get("span_width") is not None
    ]
    semantic_statuses = sorted({str(item.get("semantic_status") or "not_run") for item in pr_results})
    code_versions = Counter(str(item.get("code_version") or "unknown") for item in pr_results)
    return {
        "evaluated_instances": len(pr_results),
        "expected_notes": expected,
        "generated_notes": generated,
        "matched_line_notes": line_matched,
        "matched_semantic_notes": semantic_matched,
        "line_match_rate": line_p,
        "line_recall_rate": line_r,
        "line_f1": compute_f1(line_p, line_r),
        "semantic_match_rate": sem_p,
        "semantic_recall_rate": sem_r,
        "semantic_f1": compute_f1(sem_p, sem_r),
        "semantic_status": semantic_statuses[0] if len(semantic_statuses) == 1 else "mixed",
        "negative_line_match_count": negative_matched,
        "negative_line_match_rate": round(negative_matched / generated, 3) if generated else 0.0,
        "generated_span_width_max": max(span_widths) if span_widths else 0,
        "generated_span_width_median": (
            sorted(span_widths)[len(span_widths) // 2] if span_widths else 0
        ),
        "lost_at_counts": dict(sorted(lost_at.items())),
        "code_version_counts": dict(sorted(code_versions.items())),
    }


# ---------------------------------------------------------------------------
# Negative (label 0) comments from the annotated dataset
# ---------------------------------------------------------------------------


def load_negative_comments(csv_path: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Label-0 comments per PR from the annotated raw CSV (Hugging Face revision above)."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    with Path(csv_path).open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if str(row.get("label", "")).strip() not in {"0", "0.0"}:
                continue
            pr_url = str(row.get("pr_url") or "").strip()
            path = str(row.get("path") or "").strip()
            note = str(row.get("note") or "").strip()
            if not pr_url or not path or not note:
                continue
            from_line, to_line = normalize_line_range(
                _int_or_none(row.get("from_line")), _int_or_none(row.get("to_line"))
            )
            side = str(row.get("side") or "").strip() or None
            out.setdefault(canonical_pr_key(pr_url), []).append(
                {
                    "note": note,
                    "path": path,
                    "side": side,
                    "from_line": from_line,
                    "to_line": to_line,
                    "category": row.get("category"),
                    "is_ai_comment": row.get("is_ai_comment"),
                }
            )
    return out


# ---------------------------------------------------------------------------
# Stage attribution (local diagnostic, not an official metric)
# ---------------------------------------------------------------------------

_EXECUTOR_DECISION_RANK = {"candidate": 0, "no_finding": 1, "unsupported": 2, "budget_exhausted": 3}


def _located(obj: Any) -> tuple[str, Optional[int], Optional[int]]:
    path = normalize_path(str(_field(obj, "file_path", "") or _field(obj, "path", "") or ""))
    from_line, to_line = normalize_line_range(
        _int_or_none(_field(obj, "line_start", _field(obj, "from_line"))),
        _int_or_none(_field(obj, "line_end", _field(obj, "to_line"))),
    )
    return path, from_line, to_line


def _overlaps_reference(obj: Any, ref: Mapping[str, Any], k: int) -> bool:
    path, from_line, to_line = _located(obj)
    ref_path = normalize_path(str(ref.get("path") or ""))
    if not path or path != ref_path:
        return False
    ref_from, ref_to = ref.get("from_line"), ref.get("to_line")
    if None in (from_line, to_line, ref_from, ref_to):
        return True
    return diff_location_is_same(ref_from, ref_to, from_line, to_line, k)


def attribute_reference(
    ref: Mapping[str, Any],
    raw: Mapping[str, Any],
    *,
    final_findings: Sequence[Any] = (),
    k: int = DEFAULT_LINE_K,
) -> Dict[str, Any]:
    """Walk one reference through the run artifacts and name the stage that lost it.

    ``lost_at`` values, in pipeline order: ``no_task``, ``no_check``,
    ``check_invalid:<reason>``, ``executor:<decision|no_result>``,
    ``gate_dropped:<reason>``, ``adjudicator_<decision>``,
    ``final_consumed`` (a finding within k lines exists but another reference
    consumed it first), ``final_line_mismatch`` (a promoted finding exists
    on the path but not within k lines), ``final_missing``, and ``matched``.
    """
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {}
    ref_path = normalize_path(str(ref.get("path") or ""))

    planner = metadata.get("review_planner") if isinstance(metadata.get("review_planner"), Mapping) else {}
    task_ids = [
        str(_field(task, "id", ""))
        for task in (planner.get("tasks") or [])
        if ref_path in {normalize_path(str(p)) for p in (_field(task, "target_files", []) or [])}
    ]

    review_meta = metadata.get("review_checks") if isinstance(metadata.get("review_checks"), Mapping) else {}
    by_task = review_meta.get("by_task") if isinstance(review_meta.get("by_task"), Mapping) else {}
    compiled_ids = [
        str(_field(check, "check_id", ""))
        for slot in by_task.values()
        if isinstance(slot, Mapping)
        for check in (slot.get("compiled_checks") or [])
        if _overlaps_reference(check, ref, k)
    ]
    valid_ids = [
        str(_field(check, "check_id", ""))
        for check in (raw.get("review_checks") or [])
        if _overlaps_reference(check, ref, k)
    ]
    invalid_reasons: Counter[str] = Counter()
    for item in raw.get("invalid_review_checks") or []:
        check = _field(item, "check")
        if check is not None and _overlaps_reference(check, ref, k):
            invalid_reasons.update(str(r) for r in (_field(item, "reasons", []) or []))

    latest_by_check: Dict[str, Any] = {}
    for result in raw.get("review_check_results") or []:
        check_id = str(_field(result, "check_id", "") or "")
        if check_id:
            latest_by_check[check_id] = result
    decisions = {
        check_id: str(_field(latest_by_check[check_id], "decision", "") or "")
        for check_id in valid_ids
        if check_id in latest_by_check
    }
    contract_statuses = {
        check_id: str(_field(latest_by_check[check_id], "contract_status", "") or "")
        for check_id in valid_ids
        if check_id in latest_by_check
    }
    candidate_ids: List[str] = []
    for check_id in valid_ids:
        result = latest_by_check.get(check_id)
        candidate = _field(result, "candidate") if result is not None else None
        if candidate is not None and _overlaps_reference(candidate, ref, k):
            candidate_ids.append(str(_field(candidate, "candidate_id", "") or ""))
    for candidate in raw.get("candidate_findings") or []:
        if _overlaps_reference(candidate, ref, k):
            cid = str(_field(candidate, "candidate_id", "") or "")
            if cid and cid not in candidate_ids:
                candidate_ids.append(cid)

    gate_decisions: Dict[str, Dict[str, Any]] = {}
    for slot in by_task.values():
        gate = slot.get("gate") if isinstance(slot, Mapping) and isinstance(slot.get("gate"), Mapping) else {}
        lifecycle = gate.get("candidate_lifecycle") if isinstance(gate.get("candidate_lifecycle"), Mapping) else {}
        for cid, row in lifecycle.items():
            if cid in candidate_ids and isinstance(row, Mapping):
                gate_decisions[cid] = {"decision": row.get("decision"), "reason": row.get("reason")}

    adjudicator = metadata.get("review_adjudicator") if isinstance(metadata.get("review_adjudicator"), Mapping) else {}
    adj_lifecycle = adjudicator.get("candidate_lifecycle") if isinstance(adjudicator.get("candidate_lifecycle"), Mapping) else {}
    adjudicator_decisions = {
        cid: {"decision": row.get("decision"), "reason": row.get("reason")}
        for cid, row in adj_lifecycle.items()
        if cid in candidate_ids and isinstance(row, Mapping)
    }

    final_same_path = [
        str(_field(finding, "id", "") or "")
        for finding in final_findings
        if normalize_path(str(_field(finding, "file_path", "") or "")) == ref_path
    ]
    final_overlapping = [
        str(_field(finding, "id", "") or "")
        for finding in final_findings
        if _overlaps_reference(finding, ref, k)
    ]
    matched = bool(ref.get("line_match"))

    if matched:
        lost_at = "matched"
    elif not task_ids:
        lost_at = "no_task"
    elif not compiled_ids:
        lost_at = "no_check"
    elif not valid_ids:
        reason = invalid_reasons.most_common(1)[0][0] if invalid_reasons else "unknown"
        lost_at = f"check_invalid:{reason}"
    elif not candidate_ids:
        if decisions:
            best = min(decisions.values(), key=lambda d: _EXECUTOR_DECISION_RANK.get(d, 9))
        else:
            best = "no_result"
        lost_at = f"executor:{best or 'no_result'}"
    elif not any(row.get("decision") == "passed" for row in gate_decisions.values()):
        reasons = sorted({str(row.get("reason") or "unknown") for row in gate_decisions.values()})
        lost_at = f"gate_dropped:{reasons[0] if reasons else 'unknown'}"
    else:
        promoted = [cid for cid, row in adjudicator_decisions.items() if row.get("decision") == "promoted"]
        if promoted and final_overlapping:
            # A finding within k lines exists but another reference consumed it first.
            lost_at = "final_consumed"
        elif promoted:
            lost_at = "final_line_mismatch" if final_same_path else "final_missing"
        elif adjudicator_decisions:
            decision = sorted({str(row.get("decision") or "unknown") for row in adjudicator_decisions.values()})[0]
            lost_at = f"adjudicator_{decision}"
        else:
            lost_at = "adjudicator_missing"

    return {
        "path": ref.get("path"),
        "from_line": ref.get("from_line"),
        "to_line": ref.get("to_line"),
        "category": ref.get("category"),
        "context": ref.get("context"),
        "note": str(ref.get("note") or "")[:160],
        "lost_at": lost_at,
        "task_ids": task_ids,
        "compiled_check_ids": compiled_ids,
        "valid_check_ids": valid_ids,
        "invalid_reasons": dict(invalid_reasons),
        "executor_decisions": decisions,
        "executor_contract_status": contract_statuses,
        "candidate_ids": candidate_ids,
        "gate": gate_decisions,
        "adjudicator": adjudicator_decisions,
        "final_finding_ids_same_path": final_same_path,
        "final_finding_ids_within_k": final_overlapping,
        "line_finding_index": ref.get("line_finding_index"),
    }


# ---------------------------------------------------------------------------
# Per-PR and per-run evaluation
# ---------------------------------------------------------------------------


def evaluate_pr(
    *,
    pr_url: str,
    findings: Sequence[Any],
    references: Sequence[Mapping[str, Any]],
    raw: Optional[Mapping[str, Any]] = None,
    negatives: Optional[Sequence[Mapping[str, Any]]] = None,
    k: int = DEFAULT_LINE_K,
    judge: Optional[JudgeFn] = None,
    code_version: str = "unknown",
) -> Dict[str, Any]:
    generated = generated_comments_from_findings(findings)
    matched = match_references(references, generated, k=k, judge=judge)
    generated_count = len([g for g in generated if g.get("note")])
    statistics = compute_statistics(matched["references"], generated_count)
    attribution: List[Dict[str, Any]] = []
    if raw is not None:
        attribution = [
            attribute_reference(ref, raw, final_findings=findings, k=k) for ref in matched["references"]
        ]
    lost_at_counts = Counter(item["lost_at"] for item in attribution)
    negative_matches: List[Dict[str, Any]] = []
    if negatives:
        negative_result = match_references(negatives, generated, k=k)
        negative_matches = [
            {
                "path": row.get("path"),
                "from_line": row.get("from_line"),
                "to_line": row.get("to_line"),
                "category": row.get("category"),
                "note": str(row.get("note") or "")[:160],
                "line_finding_index": row.get("line_finding_index"),
            }
            for row in negative_result["references"]
            if row.get("line_match")
        ]
    return {
        "pr_url": pr_url,
        "line_k": k,
        # Which version of the code the reviewer saw: 'annotated_head' means the pinned
        # commit range the references were written against; 'live_head' means the PR as it
        # is today, so line numbers (and even the issues) may differ from the references.
        "code_version": code_version,
        "semantic_status": matched["semantic_status"],
        "statistics": statistics,
        "references": matched["references"],
        "generated": matched["generated"],
        "attribution": attribution,
        "lost_at_counts": dict(sorted(lost_at_counts.items())),
        "negative_line_matches": negative_matches,
        "negative_line_match_count": len(negative_matches),
    }


def token_split_from_raw(raw: Mapping[str, Any]) -> tuple[int, int]:
    """Prompt/completion token totals from successful LLM responses in a raw trace."""
    prompt = completion = 0
    for entry in raw.get("llm_trace") or []:
        if not isinstance(entry, Mapping) or entry.get("event") != "llm_response":
            continue
        usage = entry.get("token_usage") if isinstance(entry.get("token_usage"), Mapping) else {}
        prompt += int(usage.get("prompt_tokens") or 0)
        completion += int(usage.get("completion_tokens") or 0)
    return prompt, completion


def export_official(
    run_dir: Path,
    pr_results: Sequence[Mapping[str, Any]],
    references: Mapping[str, Mapping[str, Any]],
    *,
    run_id: str,
) -> Dict[str, Any]:
    """Write the official result layout so ``pipeline run --stage eval`` can score the run."""
    out_dir = Path(run_dir) / "official"
    results_dir = out_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    instances: List[Dict[str, Any]] = []
    written: List[str] = []
    skipped: List[str] = []
    for item in pr_results:
        entry = references.get(canonical_pr_key(str(item.get("pr_url") or "")))
        instance = entry.get("instance") if entry else None
        if not instance:
            skipped.append(str(item.get("pr_url")))
            continue
        payload = official_result_payload(
            item.get("findings") or [],
            duration_seconds=float(item.get("duration_seconds") or 0.0),
            input_tokens=int(item.get("input_tokens") or 0),
            output_tokens=int(item.get("output_tokens") or 0),
            run_id=run_id,
            pr_url=str(item.get("pr_url") or ""),
        )
        payload["reviewed_code_version"] = str(item.get("code_version") or "unknown")
        result_path = results_dir / f"{safe_id(instance['instance_id'])}.json"
        result_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        instances.append(instance)
        written.append(result_path.name)
    dataset_path = out_dir / "aacr_bench_slice.jsonl"
    dataset_path.write_text(
        "".join(json.dumps(instance, ensure_ascii=False) + "\n" for instance in instances),
        encoding="utf-8",
    )
    readme = out_dir / "README.md"
    readme.write_text(
        "# Official AACR-Bench evaluation input\n\n"
        f"Generated by Bushwhack run `{run_id}`. Result files follow the OCR adapter shape\n"
        "(`review.comments[]` with `path`, `start_line`, `end_line`, `content`).\n\n"
        f"Upstream evaluator: {UPSTREAM_REPO} @ `{UPSTREAM_COMMIT}`.\n\n"
        "From the upstream `evaluation/` directory (with its virtualenv and `.env` judge settings):\n\n"
        "```bash\n"
        "python -m pipeline run --stage eval --reviewer ocr \\\n"
        f"  --dataset \"{dataset_path.resolve()}\" \\\n"
        f"  --results-dir \"{results_dir.resolve()}\" \\\n"
        f"  --line-k {DEFAULT_LINE_K}\n"
        "```\n\n"
        "The dataset slice contains only the PRs in this run, so `missing_instance_ids` should be empty.\n",
        encoding="utf-8",
    )
    return {
        "results_dir": str(results_dir),
        "dataset_path": str(dataset_path),
        "written": written,
        "skipped_pr_urls": skipped,
    }


def evaluate_run(
    run_dir: Path,
    *,
    references_path: Path,
    negatives_csv: Optional[Path] = None,
    k: int = DEFAULT_LINE_K,
    judge: Optional[JudgeFn] = None,
    write: bool = True,
    export: bool = True,
) -> Dict[str, Any]:
    """Score an existing run directory from its manifest, raw traces, and findings."""
    run_dir = Path(run_dir)
    manifest_path = run_dir / "manifest.csv"
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest not found: {manifest_path}")
    run_meta_path = run_dir / "run_meta.json"
    run_id = run_dir.name
    if run_meta_path.exists():
        try:
            run_id = str(json.loads(run_meta_path.read_text(encoding="utf-8")).get("run_id") or run_id)
        except Exception:  # noqa: BLE001
            pass

    pin = dataset_pin(references_path)
    references = references_by_pr(load_reference_records(references_path)) if pin.get("exists") else {}
    negatives = load_negative_comments(negatives_csv) if negatives_csv and Path(negatives_csv).exists() else {}

    pr_results: List[Dict[str, Any]] = []
    with manifest_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        if str(row.get("status") or "") != "ok":
            continue
        pr_url = str(row.get("pr_url") or "")
        raw_path = run_dir / str(row.get("raw_path") or "")
        findings_path = run_dir / str(row.get("findings_path") or "")
        raw = json.loads(raw_path.read_text(encoding="utf-8")) if raw_path.is_file() else {}
        findings = json.loads(findings_path.read_text(encoding="utf-8")) if findings_path.is_file() else []
        entry = references.get(canonical_pr_key(pr_url), {})
        raw_metadata = raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {}
        # Runs recorded before the pinned-commit review always checked out pull/N/head.
        code_version = str(raw_metadata.get("review_code_version") or "live_head")
        result = evaluate_pr(
            pr_url=pr_url,
            findings=findings,
            references=entry.get("comments", []),
            raw=raw,
            negatives=negatives.get(canonical_pr_key(pr_url)),
            k=k,
            judge=judge,
            code_version=code_version,
        )
        prompt_tokens, completion_tokens = token_split_from_raw(raw)
        result["slug"] = row.get("slug")
        result["findings"] = findings
        result["duration_seconds"] = int(row.get("elapsed_ms") or 0) / 1000.0
        result["input_tokens"] = prompt_tokens
        result["output_tokens"] = completion_tokens
        pr_results.append(result)

    export_info = export_official(run_dir, pr_results, references, run_id=run_id) if export else None
    payload = run_evaluation_payload(
        run_id=run_id,
        pr_results=pr_results,
        dataset_pin=pin,
        official_export=export_info,
        k=k,
        judge=judge,
    )
    if write:
        write_run_evaluation(run_dir, payload)
    return payload


def run_evaluation_payload(
    *,
    run_id: str,
    pr_results: Sequence[Mapping[str, Any]],
    dataset_pin: Mapping[str, Any],
    official_export: Optional[Mapping[str, Any]],
    k: int = DEFAULT_LINE_K,
    judge: Optional[JudgeFn] = None,
) -> Dict[str, Any]:
    """The run-level ``evaluation.json`` document (findings are kept in ``findings/``)."""
    summary = summarize(pr_results)
    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "run_id": run_id,
        "line_k": k,
        "semantic_status": summary["semantic_status"],
        "semantic_judge_model": getattr(judge, "model", None),
        "upstream": {"repo": UPSTREAM_REPO, "commit": UPSTREAM_COMMIT},
        "dataset_pin": dict(dataset_pin),
        "summary": summary,
        "official_export": dict(official_export) if official_export else None,
        "prs": [
            {key: value for key, value in item.items() if key != "findings"}
            for item in pr_results
        ],
    }


def write_run_evaluation(run_dir: Path, payload: Mapping[str, Any]) -> Path:
    path = Path(run_dir) / "evaluation.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_pr_evaluation(evaluation_dir: Path, slug: str, evaluation: Mapping[str, Any]) -> Path:
    """Per-PR evaluation record without the findings (those live in ``findings/``)."""
    evaluation_dir = Path(evaluation_dir)
    evaluation_dir.mkdir(parents=True, exist_ok=True)
    path = evaluation_dir / f"{slug}.json"
    payload = {key: value for key, value in evaluation.items() if key != "findings"}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Optional semantic judge honouring the official environment contract
# ---------------------------------------------------------------------------


class OpenAICompatibleJudge:
    """The official judge call: same prompt, sampling parameters, and answer parsing."""

    def __init__(self, *, base_url: str, api_key: str, model: str, extra_body: Optional[Dict[str, Any]] = None):
        from openai import OpenAI  # imported lazily so scoring never requires the client

        self._client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model
        self.extra_body = (
            extra_body if extra_body is not None else {"chat_template_kwargs": {"enable_thinking": False}}
        )
        self.request_count = 0

    def __call__(self, reference_note: str, generated_note: str) -> bool:
        self.request_count += 1
        prompt = SEMANTIC_JUDGE_PROMPT.format(reference_note=reference_note, generated_note=generated_note)
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=40000,
                top_p=0.95,
                extra_body=self.extra_body,
            )
            result_text = (response.choices[0].message.content or "").strip().lower()
        except Exception as error:  # noqa: BLE001 - one failed judgement must not abort scoring
            logger.error("semantic judge call failed: %s", error)
            return False
        return parse_judge_answer(result_text)


def parse_judge_answer(result_text: str) -> bool:
    """Vendored answer parsing from ``judge.match_semantic``."""
    result_text = (result_text or "").strip().lower()
    return (
        "yes" in result_text
        or "similar" in result_text
        or "same" in result_text
        or "identical" in result_text
        or "equivalent" in result_text
    ) and ("no" not in result_text.split("yes")[0] if "yes" in result_text else True)


def judge_from_env(environ: Optional[Mapping[str, str]] = None) -> Optional[JudgeFn]:
    """Build the official judge from ``JUDGE_*`` variables; mock mode is never used for scoring."""
    env = environ if environ is not None else os.environ
    if str(env.get(JUDGE_USE_MOCK_VAR, "")).lower() == "true":
        return None
    api_key = env.get(JUDGE_API_KEY_VAR)
    base_url = env.get(JUDGE_BASE_URL_VAR)
    if not api_key or not base_url:
        return None
    return OpenAICompatibleJudge(
        base_url=base_url,
        api_key=api_key,
        model=env.get(JUDGE_MODEL_VAR, "multiline_judge_model"),
    )


# ---------------------------------------------------------------------------
# CLI: score an existing run directory
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Score an AACR-Bench run directory with the vendored official rules.")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--references", type=Path, default=None, help="official positive_samples.json")
    parser.add_argument("--negatives", type=Path, default=None, help="annotated raw CSV with label 0/1")
    parser.add_argument("--line-k", type=int, default=DEFAULT_LINE_K)
    parser.add_argument("--judge", choices=["none", "env"], default="none")
    parser.add_argument("--no-export", action="store_true")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[3]
    references_path = args.references or repo_root / "documentation" / "dataset" / "positive_samples.json"
    negatives_csv = args.negatives or repo_root / "data" / "raw" / "aacr_bench_raw.csv"
    judge = judge_from_env() if args.judge == "env" else None
    payload = evaluate_run(
        args.run_dir,
        references_path=references_path,
        negatives_csv=negatives_csv,
        k=args.line_k,
        judge=judge,
        export=not args.no_export,
    )
    summary = payload["summary"]
    print(json.dumps({"run_id": payload["run_id"], "dataset_pin": {
        "sha256": payload["dataset_pin"].get("sha256"),
        "matches_upstream_meta": payload["dataset_pin"].get("matches_upstream_meta"),
    }, "summary": summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
