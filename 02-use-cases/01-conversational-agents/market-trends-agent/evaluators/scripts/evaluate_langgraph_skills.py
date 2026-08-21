"""Evaluate the LangGraph Market Trends SKILL.md file-read runtime.

Usage:
    uv run python evaluators/scripts/evaluate_langgraph_skills.py \
        --region us-west-2
"""

from pathlib import Path

from evaluate_skills import main

_PROJECT_DIR = Path(__file__).resolve().parents[2]


if __name__ == "__main__":
    raise SystemExit(
        main(
            default_config=_PROJECT_DIR / "langgraph_skill_agent_config.json",
            default_results=(_PROJECT_DIR / "evaluators/results/langgraph_skill_evaluation_results.json"),
        )
    )
