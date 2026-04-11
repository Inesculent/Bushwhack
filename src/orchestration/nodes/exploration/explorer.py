from typing import Any, Dict, List
from pydantic import BaseModel, Field
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models


class ExplorerOutput(BaseModel):
	summary: str = Field(description="Short summary of the changed code and repository context.")
	insights: List[str] = Field(default_factory=list, description="Actionable review insights for planning.")
	next_step: str = Field(default="plan", description="Suggested next graph step.")


def _normalize_next_step(raw_next_step: str) -> str:
	normalized = (raw_next_step or "plan").strip().lower().replace("_", "")
	if normalized in {"explore", "plan", "review", "finalize"}:
		return normalized
	if normalized in {"endreview", "end"}:
		return "finalize"
	return "plan"


def explorer_node(state: GraphState) -> Dict[str, Any]:
	llm = Models.explorer(ExplorerOutput)

	repo_path = state.get("repo_path", "")
	git_diff = state.get("git_diff", "")
	user_goals = state.get("user_goals", "")

	prompt = (
		"You are the explorer node for a code-review orchestrator. "
		"Summarize the change context and produce concise planning insights.\n\n"
		f"Repository path: {repo_path}\n"
		f"User goals: {user_goals}\n\n"
		"Git diff:\n"
		f"{git_diff[:12000]}"
	)

	response = llm.invoke(prompt)

	metadata = dict(state.get("metadata", {}))
	metadata["explorer_summary"] = response.summary

	insights = response.insights or [response.summary]
	next_step = _normalize_next_step(response.next_step)

	return {
		"global_insights": insights,
		"next_step": next_step,
		"metadata": metadata,
		"node_history": ["explorer"],
	}
