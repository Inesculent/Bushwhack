"""Contract-justified review lens cards.

These cards are prompt fragments, not detectors. They help reviewers infer and
justify changed contracts without memorizing repository-specific issue classes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from src.domain.schemas import ReviewTask


@dataclass(frozen=True)
class LensCard:
    key: str
    question: str
    signals: tuple[str, ...]
    contract_questions: tuple[str, ...]
    counterexample_families: tuple[str, ...]
    rejection_checks: tuple[str, ...]


LENS_CARDS: tuple[LensCard, ...] = (
    LensCard(
        key="contract_delta",
        question="Contract delta: what promise changed?",
        signals=("api", "contract", "signature", "return", "caller", "public", "compat", "schema"),
        contract_questions=(
            "Which input, output, state, error, ordering, or compatibility promise is implied by names, types, callers, tests, docs, or old behavior?",
            "Does every changed path still satisfy that promise, or does the PR intentionally narrow it?",
        ),
        counterexample_families=("old caller path", "renamed or migrated path", "declared return path", "documented behavior path"),
        rejection_checks=(
            "Do not report if the PR explicitly narrows the contract and callers/tests/docs are updated.",
            "Do not infer a public contract from a private helper name alone.",
        ),
    ),
    LensCard(
        key="shape_cardinality",
        question="Shape/cardinality: are all intended items, fields, groups, or nested values preserved?",
        signals=("shape", "field", "item", "element", "list", "map", "record", "row", "group", "batch", "join", "serialize", "aggregate"),
        contract_questions=(
            "What counts as one logical item, and does the contract imply preserving one, selected, or all relevant parts?",
            "Does the changed path select, skip, replace, flatten, serialize, or aggregate only part of the promised structure?",
        ),
        counterexample_families=("empty", "one", "many", "duplicate", "nested", "optional member", "multi-field item"),
        rejection_checks=(
            "Do not report if surrounding code or docs prove only one selected part is intended.",
            "Outer container type alone does not suppress field/cardinality loss.",
        ),
    ),
    LensCard(
        key="boundary_domain",
        question="Boundary domain: what happens at null, empty, zero, one, many, invalid, duplicate, maximum, malformed, or legacy values?",
        signals=("index", "len", "count", "offset", "parse", "validate", "default", "empty", "optional", "nullable", "legacy", "malformed"),
        contract_questions=(
            "What values are valid under the declared contract, and which edge values does the changed path newly accept or reject?",
            "Does the implementation handle boundary values consistently with callers, schemas, and old behavior?",
        ),
        counterexample_families=("null/absent only when valid", "empty", "zero", "one", "many", "invalid", "duplicate", "maximum", "legacy"),
        rejection_checks=(
            "Do not report missing guards for required non-optional inputs.",
            "Do not report generic validation if caller guarantees make the value impossible.",
        ),
    ),
    LensCard(
        key="representation_fidelity",
        question="Representation fidelity: does emitted or stored data still mean what its field/name/schema says?",
        signals=("json", "serialize", "field", "id", "name", "message", "status", "header", "config", "wire", "schema", "log"),
        contract_questions=(
            "What meaning do names, wire fields, serialized forms, logs, or user-facing messages promise?",
            "Does the changed representation still round-trip, diagnose, or display the correct semantic value?",
        ),
        counterexample_families=("round trip", "missing field", "renamed field", "old config", "logged value", "user-visible message"),
        rejection_checks=(
            "Do not report style-only naming unless it affects compatibility, debugging, migration, telemetry, or users.",
        ),
    ),
    LensCard(
        key="ownership_lifecycle",
        question="Ownership/lifecycle: is every acquired resource released on success, failure, cancellation, retry, and early return?",
        signals=("open", "close", "lock", "unlock", "start", "stop", "subscribe", "unsubscribe", "allocate", "free", "retry", "cancel", "cleanup"),
        contract_questions=(
            "Who owns the acquired thing after the changed path, and what is the last safe release point?",
            "Does ownership cross loops, callbacks, retries, exceptions, cancellation, or concurrent completion?",
        ),
        counterexample_families=("success", "early error", "partial success", "retry", "cancellation", "concurrent completion"),
        rejection_checks=(
            "Do not report vague lifecycle advice; identify the specific owned object and missed or mistimed release path.",
        ),
    ),
    LensCard(
        key="time_state_freshness",
        question="Time/state freshness: can cached, captured, async, or reactive state become stale before use?",
        signals=("cache", "cached", "state", "snapshot", "async", "await", "callback", "listener", "derived", "progress", "retry"),
        contract_questions=(
            "At what time was the value captured, and at what time is it consumed?",
            "Can the changed path double-count, miss an initial/terminal state, or use a stale cleanup target?",
        ),
        counterexample_families=("stale snapshot", "late callback", "double completion", "missed initial state", "missed terminal state"),
        rejection_checks=("Do not report merely because code is async; show changed behavior from stale or mistimed state."),
    ),
    LensCard(
        key="mode_variant_completeness",
        question="Mode/variant completeness: are enum, flag, option, default, unknown, and combined cases handled consistently?",
        signals=("mode", "kind", "type", "enum", "flag", "option", "default", "case", "switch", "elif", "variant"),
        contract_questions=(
            "Which variants are supported, rejected, or defaulted by contract?",
            "Does each changed branch return, raise, serialize, and fall back consistently with sibling variants?",
        ),
        counterexample_families=("known variant", "unknown variant", "default", "combined flags", "future value", "legacy value"),
        rejection_checks=("Do not demand exhaustive handling when earlier validation intentionally rejects unknown variants."),
    ),
    LensCard(
        key="integration_surface",
        question="Integration surface: do callers, implementations, build variants, environments, persisted configs, and dependencies still fit?",
        signals=("interface", "implements", "constructor", "build", "feature", "environment", "dependency", "import", "include", "config", "migration"),
        contract_questions=(
            "What downstream code, mocks, overloads, feature-disabled builds, or older configs rely on this changed surface?",
            "Does the changed assumption hold in every supported runtime or build variant?",
        ),
        counterexample_families=("old caller", "mock implementation", "feature-disabled build", "unsupported environment", "persisted config"),
        rejection_checks=("Do not report if all relevant call sites and supported variants are updated or guarded."),
    ),
    LensCard(
        key="work_amplification",
        question="Work amplification: did expensive work move into a hot path, loop, retry, render, or large-input path?",
        signals=("loop", "retry", "cache", "allocate", "copy", "format", "render", "page", "batch", "large", "hot"),
        contract_questions=(
            "What scale or hot path does the changed work run under?",
            "Does the change repeat expensive work, leak work across iterations, or alter asymptotic behavior?",
        ),
        counterexample_families=("large input", "many items", "retry loop", "render loop", "pagination", "repeated allocation"),
        rejection_checks=("Do not report micro-optimizations without hot-path, large-input, blocking, or asymptotic evidence."),
    ),
    LensCard(
        key="diagnostic_honesty",
        question="Diagnostic honesty: do user-facing or maintainer-facing messages accurately describe behavior?",
        signals=("error", "warning", "log", "message", "tooltip", "doc", "comment", "migration", "help", "cli"),
        contract_questions=(
            "Who relies on this text, and does it still describe the behavior, type, field, command, or migration accurately?",
        ),
        counterexample_families=("wrong field", "wrong type", "wrong command", "misleading migration", "debugging search mismatch"),
        rejection_checks=("Do not report private grammar nits unless they affect user guidance, debugging, migration, or public polish."),
    ),
)

_DEFAULT_KEYS = ("contract_delta", "shape_cardinality", "mode_variant_completeness", "integration_surface")


def _blob_for_selection(
    *,
    task: ReviewTask | None = None,
    text: str = "",
    obligations: Sequence[Mapping[str, object]] = (),
) -> str:
    parts: list[str] = [text]
    if task is not None:
        parts.append(f"{task.title} {task.description} {task.specialty} {' '.join(task.target_files)}")
    for row in obligations:
        parts.append(" ".join(str(row.get(key) or "") for key in ("surface", "dimension", "evidence")))
    return " ".join(parts).lower()


def _score_card(card: LensCard, blob: str) -> int:
    return sum(1 for signal in card.signals if signal.lower() in blob)


def lens_card_selection_diagnostics(
    *,
    task: ReviewTask | None = None,
    text: str = "",
    obligations: Sequence[Mapping[str, object]] = (),
    max_cards: int = 4,
) -> dict[str, object]:
    blob = _blob_for_selection(task=task, text=text, obligations=obligations)
    scored = [
        {
            "key": card.key,
            "score": _score_card(card, blob),
            "matched_signals": [signal for signal in card.signals if signal.lower() in blob],
        }
        for card in LENS_CARDS
    ]
    selected = select_lens_cards(
        task=task,
        text=text,
        obligations=obligations,
        max_cards=max_cards,
    )
    return {
        "selected_keys": [card.key for card in selected],
        "max_cards": max(1, max_cards),
        "obligation_count": len(obligations),
        "scores": [row for row in scored if int(row["score"]) > 0],
        "used_default": not any(int(row["score"]) > 0 for row in scored),
    }


def select_lens_cards(
    *,
    task: ReviewTask | None = None,
    text: str = "",
    obligations: Sequence[Mapping[str, object]] = (),
    max_cards: int = 4,
) -> list[LensCard]:
    blob = _blob_for_selection(task=task, text=text, obligations=obligations)
    scored = [(_score_card(card, blob), index, card) for index, card in enumerate(LENS_CARDS)]
    selected = [card for score, _index, card in sorted(scored, key=lambda item: (-item[0], item[1])) if score > 0]
    if not selected:
        selected = [card for key in _DEFAULT_KEYS for card in LENS_CARDS if card.key == key]
    return selected[: max(1, max_cards)]


def format_lens_question_list() -> str:
    return "\n".join(f"- {card.question}" for card in LENS_CARDS)


def format_lens_cards(cards: Iterable[LensCard]) -> str:
    blocks: list[str] = []
    for card in cards:
        blocks.append(
            "\n".join(
                [
                    f"### {card.question}",
                    "Contract questions:",
                    *[f"- {item}" for item in card.contract_questions],
                    "Counterexample families:",
                    *[f"- {item}" for item in card.counterexample_families],
                    "Rejection/suppression checks:",
                    *[f"- {item}" for item in card.rejection_checks],
                ]
            )
        )
    return "\n\n".join(blocks)
