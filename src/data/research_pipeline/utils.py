from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import pandas as pd


REPO_FROM_PR_URL_PATTERN = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)")


def ensure_directories(paths: Iterable[Path]) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def normalize_language(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def is_target_language(value: object, target_languages: set[str]) -> bool:
    return normalize_language(value) in target_languages


def coalesce_columns(df: pd.DataFrame, candidates: list[str], default: object = None) -> pd.Series:
    for column in candidates:
        if column in df.columns:
            return df[column]
    return pd.Series([default] * len(df), index=df.index)


def to_numeric(series: pd.Series, default: int = 0) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    return numeric.fillna(default)


def parse_repo_from_pr_url(pr_url: str | None) -> str | None:
    if not pr_url or not isinstance(pr_url, str):
        return None
    parsed = urlparse(pr_url)
    match = REPO_FROM_PR_URL_PATTERN.match(parsed.path)
    if not match:
        return None
    owner = match.group("owner")
    repo = match.group("repo")
    return f"{owner}/{repo}"


def parse_pr_number(pr_url: str | None) -> int | None:
    if not pr_url or not isinstance(pr_url, str):
        return None
    parsed = urlparse(pr_url)
    match = REPO_FROM_PR_URL_PATTERN.match(parsed.path)
    if not match:
        return None
    return int(match.group("number"))


def repo_to_url(repo: str | None) -> str | None:
    if not repo or not isinstance(repo, str):
        return None
    repo = repo.strip("/")
    if "/" not in repo:
        return None
    return f"https://github.com/{repo}"


def write_raw_snapshot(df: pd.DataFrame, path: Path) -> None:
    df.to_csv(path, index=False)
