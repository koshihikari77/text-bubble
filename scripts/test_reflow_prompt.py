#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from bubble_pipeline import infer_assignment_plans, infer_reflow_plans  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the reflow prompt against multiple text cases.")
    parser.add_argument("--server", default="http://127.0.0.1:8080/v1", help="llama-server base URL")
    parser.add_argument("--model", default="heretic", help="Model alias exposed by llama-server")
    parser.add_argument("--temperature", default=0.0, type=float, help="Sampling temperature")
    parser.add_argument(
        "--cases-file",
        default=str(ROOT_DIR / "prompts" / "reflow_test_cases.json"),
        help="JSON file containing an array of test strings",
    )
    parser.add_argument("--limit", type=int, help="Optional maximum number of cases to run")
    parser.add_argument("--indent", default=2, type=int, help="JSON output indentation")
    return parser.parse_args()


def load_cases(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(item, str) and item.strip() for item in data):
        raise RuntimeError(f"cases file must be a JSON array of non-empty strings: {path}")
    return [item.strip() for item in data]


def main() -> int:
    args = parse_args()
    cases_path = Path(args.cases_file)
    if not cases_path.is_file():
        raise RuntimeError(f"cases file not found: {cases_path}")

    cases = load_cases(cases_path)
    if args.limit is not None:
        cases = cases[: max(0, args.limit)]
    if not cases:
        raise RuntimeError("no test cases to run")

    results = []
    for index, text in enumerate(cases, start=1):
        started_at = time.monotonic()
        dialogue_lines, assignment_plans = infer_assignment_plans(text)
        _, reflow_plans = infer_reflow_plans(
            server=args.server,
            model=args.model,
            dialogue=text,
            temperature=args.temperature,
            assignment_plans=assignment_plans,
        )
        plan = reflow_plans[0]
        results.append(
            {
                "index": index,
                "input": text,
                "bubble_id": plan.bubble_id,
                "columns": plan.columns,
                "reconstructed": "".join(plan.columns),
                "num_columns": len(plan.columns),
                "elapsed_sec": round(time.monotonic() - started_at, 3),
            }
        )

    print(
        json.dumps(
            {
                "server": args.server,
                "model": args.model,
                "temperature": args.temperature,
                "cases_file": str(cases_path),
                "count": len(results),
                "results": results,
            },
            ensure_ascii=False,
            indent=args.indent,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
