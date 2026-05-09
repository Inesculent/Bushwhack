import sys
import os
import logging
from typing import Any, Dict, List, Optional

import requests
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv, find_dotenv

# 1. Route logs to stderr to protect the standard output JSON stream
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("github-mcp")

# 2. Setup Authentication

load_dotenv(find_dotenv())
GITHUB_TOKEN = os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN")
if not GITHUB_TOKEN:
    logger.error("Fatal: GITHUB_PERSONAL_ACCESS_TOKEN environment variable is missing.")
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "X-GitHub-Api-Version": "2022-11-28"
}

# 3. Initialize Server
mcp = FastMCP("GitHub-Agentic-Review-Server")

# 4. Define Tools
@mcp.tool()
def get_repo_structure(owner: str, repo: str, path: str = "", ref: str = "") -> Dict[str, Any]:
    """Fetch a directory listing for a GitHub repository. Leave path empty for root."""
    logger.info("Fetching repo structure for %s/%s/%s", owner, repo, path)
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    params = {"ref": ref} if ref else None

    response = requests.get(url, headers=HEADERS, params=params)
    if response.status_code != 200:
        return {
            "error": response.json().get("message", response.text),
            "owner": owner,
            "repo": repo,
            "path": path,
            "ref": ref or None,
            "entries": [],
        }

    contents = response.json()
    if not isinstance(contents, list):
        contents = [contents]

    entries = [
        {
            "type": item.get("type"),
            "path": item.get("path"),
            "name": item.get("name"),
            "sha": item.get("sha"),
        }
        for item in contents
    ]
    return {
        "owner": owner,
        "repo": repo,
        "path": path,
        "ref": ref or None,
        "entries": entries,
    }

@mcp.tool()
def get_file_content(owner: str, repo: str, path: str, ref: str = "main") -> Dict[str, Any]:
    """Fetch the raw contents of a specific file for code analysis."""
    logger.info("Fetching file: %s from %s/%s (ref=%s)", path, owner, repo, ref)
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"

    response = requests.get(url, headers=HEADERS)
    if response.status_code != 200:
        return {
            "error": f"HTTP {response.status_code}",
            "owner": owner,
            "repo": repo,
            "path": path,
            "ref": ref,
            "content": "",
        }

    return {
        "owner": owner,
        "repo": repo,
        "path": path,
        "ref": ref,
        "content": response.text,
    }

@mcp.tool()
def get_pull_request_diff(owner: str, repo: str, pull_number: int) -> str:
    """Fetches the raw diff of a Pull Request so the agent can review the exact code changes."""
    logger.info(f"Fetching PR #{pull_number} diff for {owner}/{repo}")
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}"
    
    # We specifically request the diff format from GitHub
    diff_headers = HEADERS.copy()
    diff_headers["Accept"] = "application/vnd.github.v3.diff"
    
    response = requests.get(url, headers=diff_headers)
    if response.status_code != 200:
        return f"Error fetching PR diff: {response.text}"
        
    return f"--- Diff for PR #{pull_number} ---\n{response.text}"


@mcp.tool()
def get_pull_request(owner: str, repo: str, pull_number: int) -> Dict[str, Any]:
    """Fetch basic pull request metadata (title, body, refs)."""
    logger.info("Fetching PR #%s metadata for %s/%s", pull_number, owner, repo)
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}"

    response = requests.get(url, headers=HEADERS)
    if response.status_code != 200:
        return {
            "error": response.json().get("message", response.text),
            "owner": owner,
            "repo": repo,
            "number": pull_number,
        }

    payload = response.json()
    return {
        "owner": owner,
        "repo": repo,
        "number": pull_number,
        "title": payload.get("title") or "",
        "body": payload.get("body") or "",
        "html_url": payload.get("html_url"),
        "state": payload.get("state"),
        "base_ref": payload.get("base", {}).get("ref"),
        "head_ref": payload.get("head", {}).get("ref"),
        "author": payload.get("user", {}).get("login"),
    }


@mcp.tool()
def get_issue(owner: str, repo: str, issue_number: int) -> Dict[str, Any]:
    """Fetch issue metadata (title/body) by number."""
    logger.info("Fetching issue #%s for %s/%s", issue_number, owner, repo)
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}"

    response = requests.get(url, headers=HEADERS)
    if response.status_code != 200:
        return {
            "error": response.json().get("message", response.text),
            "owner": owner,
            "repo": repo,
            "number": issue_number,
        }

    payload = response.json()
    return {
        "owner": owner,
        "repo": repo,
        "number": issue_number,
        "title": payload.get("title") or "",
        "body": payload.get("body") or "",
        "html_url": payload.get("html_url"),
        "state": payload.get("state"),
    }


@mcp.tool()
def get_issue_comments(owner: str, repo: str, issue_number: int, limit: int = 20) -> Dict[str, Any]:
    """Fetch a bounded list of comments for an issue or PR (issues API)."""
    logger.info("Fetching issue #%s comments for %s/%s", issue_number, owner, repo)
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments"
    params = {"per_page": max(1, min(limit, 100))}

    response = requests.get(url, headers=HEADERS, params=params)
    if response.status_code != 200:
        return {
            "error": response.json().get("message", response.text),
            "owner": owner,
            "repo": repo,
            "number": issue_number,
            "comments": [],
        }

    payload: List[Dict[str, Any]] = response.json() if isinstance(response.json(), list) else []
    comments = [
        {
            "author": item.get("user", {}).get("login"),
            "body": item.get("body") or "",
            "html_url": item.get("html_url"),
            "created_at": item.get("created_at"),
        }
        for item in payload
    ]
    return {
        "owner": owner,
        "repo": repo,
        "number": issue_number,
        "comments": comments,
    }

@mcp.tool()
def create_pr_review_comment(owner: str, repo: str, pull_number: int, body: str) -> str:
    """Posts a general review comment on a Pull Request."""
    logger.info(f"Posting review comment to PR #{pull_number} on {owner}/{repo}")
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pull_number}/comments"
    
    response = requests.post(url, headers=HEADERS, json={"body": body})
    if response.status_code == 201:
        return f"Successfully posted comment to PR #{pull_number}. URL: {response.json().get('html_url')}"
    else:
        return f"Failed to post comment: {response.json().get('message', response.text)}"

if __name__ == "__main__":
    logger.info("Initializing Custom GitHub MCP Server...")
    mcp.run(transport='stdio')