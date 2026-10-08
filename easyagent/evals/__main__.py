"""python -m easyagent.evals run --connection <name>"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from easyagent.evals.runner import compare_results, format_compare, format_summary, run_suite


def _build() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m easyagent.evals", description="Run or compare EasyAgent evals.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run tasks against one connection.")
    run.add_argument("--connection", default="", help="Saved connection name. Not used with --mock.")
    run.add_argument("--tasks", default="", help="Comma-separated task ids. Default is every task.")
    run.add_argument(
        "--judge-model",
        default="",
        help="Model name for rubric grading on this same connection. Default is the bot's model.",
    )
    run.add_argument("--mock", action="store_true", help="Scripted model. No server is contacted.")
    run.add_argument("--data", default="", help="Folder with endpoints.json. Read only. Default is EASYAGENT_DATA or ./data.")
    run.add_argument("--out", default="", help="Folder for the result JSON. Default is ./evals/results.")

    diff = sub.add_parser("compare", help="Flag tasks that passed in the first file and fail in the second.")
    diff.add_argument("before", type=Path)
    diff.add_argument("after", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build()
    args = parser.parse_args(argv)
    if args.command == "compare":
        before = json.loads(args.before.read_text(encoding="utf-8"))
        after = json.loads(args.after.read_text(encoding="utf-8"))
        report = compare_results(before, after)
        print(format_compare(report))
        return 0 if not report["regressions"] else 1
    if not args.mock and not (args.connection or "").strip():
        parser.error("run needs --connection or --mock")
    ids = [item.strip() for item in (args.tasks or "").split(",") if item.strip()]
    try:
        report = run_suite(
            connection=args.connection,
            task_ids=ids or None,
            judge_model=args.judge_model,
            mock=bool(args.mock),
            data_dir=args.data or None,
            out_dir=args.out or None,
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(format_summary(report))
    print(report["path"])
    failed = report["summary"]["total"] - report["summary"]["passed"]
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
