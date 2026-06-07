"""Shared contract vocabulary for mental-model and check framing."""

from __future__ import annotations

import re
from typing import Iterable

DATA_SHAPE_CONTRACT_TERMS = frozenset(
    {
        "aggregate",
        "aggregation",
        "array",
        "batch",
        "batches",
        "collection",
        "complete",
        "data",
        "dict",
        "each",
        "element",
        "elements",
        "every",
        "field",
        "fields",
        "flatten",
        "full",
        "group",
        "grouped",
        "groups",
        "items",
        "list",
        "map",
        "mapping",
        "mappings",
        "matches",
        "mode",
        "parse",
        "record",
        "records",
        "row",
        "rows",
        "serialize",
        "shape",
        "slot",
        "slots",
        "structured",
        "template",
        "tuple",
    }
)

COMPLETENESS_CONTRACT_TERMS = frozenset(
    {
        "all",
        "batch",
        "batches",
        "cardinality",
        "collection",
        "complete",
        "completeness",
        "each",
        "element",
        "elements",
        "every",
        "field",
        "fields",
        "group",
        "grouped",
        "groups",
        "item",
        "items",
        "mapping",
        "mappings",
        "record",
        "records",
        "row",
        "rows",
        "slot",
        "slots",
    }
)


def contract_tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z0-9_]*", text.lower()))


def has_any_contract_term(text: str, terms: Iterable[str]) -> bool:
    return bool(contract_tokens(text) & set(terms))
