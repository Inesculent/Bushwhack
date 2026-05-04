import argparse
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))

from src.domain.state import GraphState
from src.orchestration.graph import run_baseline


def _read_diff(diff_file: str | None) -> str:
	if not diff_file:
		return ""
	return Path(diff_file).read_text(encoding="utf-8")


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Run the baseline one-node LangGraph flow.")
	parser.add_argument("--repo-path", default=".", help="Absolute or relative repository path.")
	parser.add_argument(
		"--diff-file",
		default=None,
		help="Optional path to a text file containing git diff content.",
	)
	parser.add_argument(
		"--user-goals",
		default="Baseline exploration run",
		help="Optional high-level goals for the review.",
	)
	return parser.parse_args()


def main() -> None:
	args = parse_args()
	initial_state: GraphState = {
		"run_id": str(uuid.uuid4()),
		"repo_path": str(Path(args.repo_path).resolve()),
		"git_diff": _read_diff(args.diff_file),
		"user_goals": args.user_goals,
		"global_insights": [],
		"findings": [],
		"token_usage": 0,
		"node_history": [],
	}

	result = run_baseline(initial_state)
	print("run_id:", result.get("run_id"))
	print("next_step:", result.get("next_step"))
	print("node_history:", result.get("node_history"))
	print("global_insights:", result.get("global_insights"))
	print("metadata:", result.get("metadata"))


if __name__ == "__main__":
	main()
