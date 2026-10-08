"""Eval runner. Tasks live in evals/tasks. Results are scores, not chats."""

from easyagent.evals.runner import compare_results, format_summary, run_suite

__all__ = ["compare_results", "format_summary", "run_suite"]
