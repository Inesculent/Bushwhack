from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable

import requests

from .utils import parse_pr_number, parse_repo_from_pr_url


@dataclass(frozen=True, slots=True)
class PullRequestMetrics:
    pr_url: str
    repo: str
    pr_files_changed: int
    pr_lines_added: int
    pr_lines_removed: int
    pr_total_lines_changed: int
    repo_size_kb: int
    repo_total_files: int
    repo_python_files: int
    repo_total_directories: int
    repo_max_directory_depth: int


@dataclass(frozen=True, slots=True)
class RepoStructureMetrics:
    repo: str
    repo_total_files: int
    repo_python_files: int
    repo_total_directories: int
    repo_max_directory_depth: int


class GitHubPullRequestEnricher:
    def __init__(self, logger: logging.Logger, token: str | None = None, timeout_seconds: int = 30) -> None:
        self._logger = logger
        self._timeout_seconds = timeout_seconds
        self._session = requests.Session()
        self._repo_size_cache: dict[str, int | None] = {}
        self._repo_structure_cache: dict[str, RepoStructureMetrics | None] = {}
        self._repo_metadata_cache: dict[str, dict[str, object] | None] = {}
        self._session.headers.update({"Accept": "application/vnd.github+json"})
        if token:
            self._session.headers.update({"Authorization": f"Bearer {token}"})

    def fetch_repo_sizes(self, repos: Iterable[str]) -> dict[str, int]:
        unique_repos = sorted({repo for repo in repos if isinstance(repo, str) and repo.strip()})
        sizes: dict[str, int] = {}
        self._logger.info("Starting repository metadata enrichment for %s repos", len(unique_repos))

        for idx, repo in enumerate(unique_repos, start=1):
            if idx % 50 == 0:
                self._logger.info("Repository enrichment progress: %s/%s", idx, len(unique_repos))

            size_kb = self._fetch_repo_size(repo)
            if size_kb is not None:
                sizes[repo] = size_kb

        self._logger.info("Repository metadata enrichment finished with %s rows", len(sizes))
        return sizes

    def fetch_repo_structure_bulk(self, repos: Iterable[str]) -> dict[str, RepoStructureMetrics]:
        unique_repos = sorted({repo for repo in repos if isinstance(repo, str) and repo.strip()})
        structures: dict[str, RepoStructureMetrics] = {}
        self._logger.info("Starting repository structure enrichment for %s repos", len(unique_repos))

        for idx, repo in enumerate(unique_repos, start=1):
            if idx % 50 == 0:
                self._logger.info("Repository structure enrichment progress: %s/%s", idx, len(unique_repos))

            metrics = self._fetch_repo_structure_metrics(repo)
            if metrics is not None:
                structures[repo] = metrics

        self._logger.info("Repository structure enrichment finished with %s rows", len(structures))
        return structures

    def fetch_bulk(self, pr_urls: Iterable[str]) -> list[PullRequestMetrics]:
        unique_urls = sorted({url for url in pr_urls if isinstance(url, str) and url.strip()})
        metrics: list[PullRequestMetrics] = []
        self._logger.info("Starting GitHub enrichment for %s unique PR URLs", len(unique_urls))

        for idx, pr_url in enumerate(unique_urls, start=1):
            if idx % 50 == 0:
                self._logger.info("GitHub enrichment progress: %s/%s", idx, len(unique_urls))

            metric = self._fetch_one(pr_url)
            if metric is not None:
                metrics.append(metric)

        self._logger.info("GitHub enrichment finished with %s successful PR metric rows", len(metrics))
        return metrics

    def _fetch_one(self, pr_url: str) -> PullRequestMetrics | None:
        repo = parse_repo_from_pr_url(pr_url)
        pr_number = parse_pr_number(pr_url)
        if repo is None or pr_number is None:
            self._logger.warning("Skipping malformed PR URL: %s", pr_url)
            return None

        api_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
        while True:
            response = self._session.get(api_url, timeout=self._timeout_seconds)

            if response.status_code == 404:
                self._logger.warning("Skipping missing PR (404): %s", pr_url)
                return None

            if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
                reset_epoch = int(response.headers.get("X-RateLimit-Reset", "0"))
                sleep_seconds = max(reset_epoch - int(time.time()), 0) + 1
                self._logger.warning(
                    "GitHub rate limit hit; sleeping for %s seconds before retrying %s",
                    sleep_seconds,
                    pr_url,
                )
                time.sleep(sleep_seconds)
                continue

            if response.status_code >= 400:
                self._logger.warning("Skipping PR %s due to status code %s", pr_url, response.status_code)
                return None

            payload = response.json()
            additions = int(payload.get("additions", 0) or 0)
            deletions = int(payload.get("deletions", 0) or 0)
            changed_files = int(payload.get("changed_files", 0) or 0)
            repo_size_kb = self._fetch_repo_size(repo)
            repo_structure = self._fetch_repo_structure_metrics(repo)
            return PullRequestMetrics(
                pr_url=pr_url,
                repo=repo,
                pr_files_changed=changed_files,
                pr_lines_added=additions,
                pr_lines_removed=deletions,
                pr_total_lines_changed=additions + deletions,
                repo_size_kb=int(repo_size_kb or 0),
                repo_total_files=int(repo_structure.repo_total_files if repo_structure else 0),
                repo_python_files=int(repo_structure.repo_python_files if repo_structure else 0),
                repo_total_directories=int(repo_structure.repo_total_directories if repo_structure else 0),
                repo_max_directory_depth=int(repo_structure.repo_max_directory_depth if repo_structure else 0),
            )

    def _fetch_repo_metadata(self, repo: str) -> dict[str, object] | None:
        if repo in self._repo_metadata_cache:
            return self._repo_metadata_cache[repo]

        api_url = f"https://api.github.com/repos/{repo}"
        while True:
            response = self._session.get(api_url, timeout=self._timeout_seconds)

            if response.status_code == 404:
                self._logger.warning("Skipping missing repository metadata (404): %s", repo)
                self._repo_metadata_cache[repo] = None
                return None

            if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
                reset_epoch = int(response.headers.get("X-RateLimit-Reset", "0"))
                sleep_seconds = max(reset_epoch - int(time.time()), 0) + 1
                self._logger.warning(
                    "GitHub rate limit hit; sleeping for %s seconds before retrying repo metadata %s",
                    sleep_seconds,
                    repo,
                )
                time.sleep(sleep_seconds)
                continue

            if response.status_code >= 400:
                self._logger.warning("Skipping repository %s metadata due to status code %s", repo, response.status_code)
                self._repo_metadata_cache[repo] = None
                return None

            payload = response.json()
            self._repo_metadata_cache[repo] = payload
            return payload

    def _fetch_repo_size(self, repo: str) -> int | None:
        if repo in self._repo_size_cache:
            return self._repo_size_cache[repo]

        metadata = self._fetch_repo_metadata(repo)
        if metadata is None:
            self._repo_size_cache[repo] = None
            return None

        size_kb = int(metadata.get("size", 0) or 0)
        self._repo_size_cache[repo] = size_kb
        return size_kb

    def _fetch_repo_structure_metrics(self, repo: str) -> RepoStructureMetrics | None:
        if repo in self._repo_structure_cache:
            return self._repo_structure_cache[repo]

        metadata = self._fetch_repo_metadata(repo)
        if metadata is None:
            self._repo_structure_cache[repo] = None
            return None

        default_branch = str(metadata.get("default_branch", "")).strip()
        if not default_branch:
            self._logger.warning("Skipping repository structure metrics for %s because default branch is missing", repo)
            self._repo_structure_cache[repo] = None
            return None

        api_url = f"https://api.github.com/repos/{repo}/git/trees/{default_branch}?recursive=1"
        while True:
            response = self._session.get(api_url, timeout=self._timeout_seconds)

            if response.status_code == 404:
                self._logger.warning("Skipping repository tree metrics (404): %s", repo)
                self._repo_structure_cache[repo] = None
                return None

            if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
                reset_epoch = int(response.headers.get("X-RateLimit-Reset", "0"))
                sleep_seconds = max(reset_epoch - int(time.time()), 0) + 1
                self._logger.warning(
                    "GitHub rate limit hit; sleeping for %s seconds before retrying repo tree %s",
                    sleep_seconds,
                    repo,
                )
                time.sleep(sleep_seconds)
                continue

            if response.status_code >= 400:
                self._logger.warning("Skipping repository %s tree due to status code %s", repo, response.status_code)
                self._repo_structure_cache[repo] = None
                return None

            payload = response.json()
            tree_entries = payload.get("tree", [])
            if payload.get("truncated", False):
                self._logger.warning("Repository tree for %s is truncated by GitHub API; structure metrics may be partial", repo)

            total_files = 0
            python_files = 0
            total_directories = 0
            max_directory_depth = 0

            for entry in tree_entries:
                entry_type = str(entry.get("type", ""))
                path = str(entry.get("path", ""))

                if entry_type == "blob":
                    total_files += 1
                    if path.lower().endswith(".py"):
                        python_files += 1
                    file_dir_depth = path.count("/")
                    if file_dir_depth > max_directory_depth:
                        max_directory_depth = file_dir_depth
                elif entry_type == "tree":
                    total_directories += 1
                    dir_depth = path.count("/") + 1 if path else 0
                    if dir_depth > max_directory_depth:
                        max_directory_depth = dir_depth

            metrics = RepoStructureMetrics(
                repo=repo,
                repo_total_files=total_files,
                repo_python_files=python_files,
                repo_total_directories=total_directories,
                repo_max_directory_depth=max_directory_depth,
            )
            self._repo_structure_cache[repo] = metrics
            return metrics
