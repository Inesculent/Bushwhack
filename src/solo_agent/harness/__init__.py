"""Dataset-specific harnesses that drive the solo-agent graph.

The harness layer is where "for this dataset, how do we source ``pr_title``,
``pr_description``, and the unified diff" lives. The worker itself is dataset
agnostic. Adding a new dataset means adding a sibling module here, not touching
``src.solo_agent.worker`` or ``src.solo_agent.graph``.
"""
