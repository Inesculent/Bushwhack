from abc import ABC, abstractmethod
from pydantic import BaseModel
from typing import List, Optional, Any, Dict, Sequence
from .schemas import (
    CodeEntity,
    DiffManifest,
    GitHubIssueContext,
    GitHubIssueComment,
    GitHubPullRequestContext,
    RepoDocsBundle,
    SearchResult,
    PreflightRequest,
)

"""Domain ports.

Contract:
- repository_path is always an absolute filesystem path.
- file_path values are repository-relative paths using '/' separators.
"""


class ICodeSearcher(ABC):
    @abstractmethod
    def search_text(
        self,
        query: str,
        repository_path: str,
        file_paths: Sequence[str] | None = None,
    ) -> List[SearchResult]:
        """
        Search in the specified repository and return repository-relative matches.
        """
        pass

    @abstractmethod
    def find_symbol(self, symbol_name: str, repository_path: str) -> List[SearchResult]:
        """
        Resolve symbol matches in the specified repository.
        """
        pass


class IASTParser(ABC):
    @abstractmethod
    def get_file_structure(self, repository_path: str, file_path: str) -> List[CodeEntity]:
        """
        Return all entities in the given repository-relative file.
        """
        pass

    @abstractmethod
    def get_entity_details(
        self,
        repository_path: str,
        file_path: str,
        entity_name: str,
    ) -> Optional[CodeEntity]:
        """
        Retrieve details for an entity in the given repository-relative file.
        """
        pass


class ICacheService(ABC):
    @abstractmethod
    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve a value from the cache using the specified key.
        """
        pass

    @abstractmethod
    def set(self, key: str, value: Dict[str, Any], expire: int = 3600) -> None:
        """
        Store a value in the cache with the specified key and expiration time.
        """
        pass

    @abstractmethod
    def delete(self, key: str) -> None:
        """
        Delete a value from the cache using the specified key.
        """
        pass

    @abstractmethod
    def exists(self, key: str) -> bool:
        """
        Check whether a value exists in the cache for the specified key.
        """
        pass


class ILLMService(ABC):
    @abstractmethod
    def complete(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        """
        Generate a plain-text completion for the given prompt.
        """
        pass

    @abstractmethod
    def complete_structured(
        self,
        prompt: str,
        response_model: type[BaseModel],
        system_prompt: Optional[str] = None,
    ) -> BaseModel:
        """
        Generate a structured completion matching the provided Pydantic response model.
        """
        pass


class IPreflightService(ABC):
    @abstractmethod
    def build_diff_manifest(self, request: PreflightRequest) -> DiffManifest:
        """
        Build a deterministic diff manifest from run metadata and diff payload input.
        """
        pass


class IGitHubContextProvider(ABC):
    @abstractmethod
    def get_repo_docs(
        self,
        owner: str,
        repo: str,
        ref: str,
        paths: Sequence[str],
    ) -> RepoDocsBundle:
        """
        Fetch a bounded bundle of documentation files from the repository.
        """
        pass

    @abstractmethod
    def get_pull_request(
        self,
        owner: str,
        repo: str,
        pull_number: int,
    ) -> GitHubPullRequestContext | None:
        """
        Fetch basic pull request metadata including title, body, and refs.
        """
        pass

    @abstractmethod
    def get_issue(
        self,
        owner: str,
        repo: str,
        issue_number: int,
    ) -> GitHubIssueContext | None:
        """
        Fetch a linked issue summary (title/body) when available.
        """
        pass

    @abstractmethod
    def get_issue_comments(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        limit: int,
    ) -> List[GitHubIssueComment]:
        """
        Fetch a bounded list of comments for a PR or issue.
        """
        pass

