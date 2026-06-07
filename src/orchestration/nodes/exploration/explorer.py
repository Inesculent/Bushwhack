from typing import Any, Dict, List
from pydantic import BaseModel, Field
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.token_usage import parse_structured_output
from src.infrastructure.llm.trace import trace_llm_call
from src.orchestration.prompts.exploration_prompts import render_explorer_prompt


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

	prompt = render_explorer_prompt(repo_path=repo_path, user_goals=user_goals, git_diff=git_diff)

	traced = trace_llm_call(
		llm,
		prompt,
		state=state,
		node_name="explorer",
		schema_name="ExplorerOutput",
		input_summary={"repo_path": repo_path},
	)
	invoke_result = traced.result
	response = parse_structured_output(invoke_result, ExplorerOutput)
	tokens = traced.tokens

	metadata = dict(state.get("metadata", {}))
	metadata["explorer_summary"] = response.summary

	insights = response.insights or [response.summary]
	next_step = _normalize_next_step(response.next_step)

	return {
		"global_insights": insights,
		"next_step": next_step,
		"metadata": metadata,
		"node_history": ["explorer"],
		"token_usage": tokens,
		"llm_trace": traced.trace_records,
	}
