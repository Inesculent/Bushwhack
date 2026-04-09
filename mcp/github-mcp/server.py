import sys
import os
import logging
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
def get_repo_structure(owner: str, repo: str, path: str = "") -> str:
    """Fetches the directory structure of a GitHub repository to help map the codebase. Leave path empty for root."""
    logger.info(f"Fetching repo structure for {owner}/{repo}/{path}")
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    
    response = requests.get(url, headers=HEADERS)
    if response.status_code != 200:
        return f"Error accessing repo: {response.json().get('message', response.text)}"
        
    contents = response.json()
    if not isinstance(contents, list):
        contents = [contents] # Handle single file edge case
        
    tree = [f"- [{item['type']}] {item['path']}" for item in contents]
    return f"Directory Structure for {owner}/{repo}/{path}:\n" + "\n".join(tree)

@mcp.tool()
def get_file_content(owner: str, repo: str, path: str) -> str:
    """Fetches the raw contents of a specific file for code analysis."""
    logger.info(f"Fetching file: {path} from {owner}/{repo}")
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/main/{path}"
    
    response = requests.get(url, headers=HEADERS)
    if response.status_code != 200:
        return f"Error fetching file (ensure path is correct and file exists on main branch): {response.status_code}"
        
    return f"--- {path} ---\n{response.text}"

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