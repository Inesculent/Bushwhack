import argparse
import sys
import subprocess
import json
import urllib.request
import urllib.error

AGENT_API_URL = "http://localhost:8000/review"

def run_cmd(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.stdout.strip()

def main():
    parser = argparse.ArgumentParser(description="Code Review Agent")
    parser.add_argument('--review', action='store_true', help='Trigger an agentic code review of staged changes')
    args = parser.parse_args()

    if not args.review:
        parser.print_help()
        sys.exit(0)
    
    diff = run_cmd("git diff --cached")

    if not diff:
        print("No staged changes found")
        sys.exit(0)
    
    repo_path = run_cmd("git rev-parse --show-toplevel")
    branch = run_cmd("git rev-parse --abbrev-ref HEAD")

    payload = {
        "repository_path": repo_path,
        "branch": branch,
        "diff": diff
    }

    data = json.dumps(payload).encode('utf-8')
    
    req = urllib.request.Request(
        AGENT_API_URL, 
        data=data, 
        headers={'Content-Type': 'application/json'}
    )

    try:
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode('utf-8'))

            if result.get('status') == 'approved':
                print("Code review approved")
            else:
                print("Code review failed")
    
    except urllib.error.URLError as e:
        print(f"Error: {e.reason}")

if __name__ == "__main__":
    main()