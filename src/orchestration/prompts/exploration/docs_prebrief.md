You are drafting a documentation-based pre-brief for reviewers.

Goal:
- Provide a concise proposed understanding of the repository based on the supplied documentation and PR context.
- Highlight critical workflows, integration boundaries, and any stated constraints or policies.
- If information is missing or ambiguous, state that clearly instead of guessing.

Output format (JSON):
- summary: string
- insights: list of short, actionable statements (max 5)

Context:
Repository: {repo_path}

Documentation:
{docs}

Pull Request Context:
{pr_context}

Linked Issues:
{issues}

PR Comments:
{comments}
