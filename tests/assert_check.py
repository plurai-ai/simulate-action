"""Asserts that the check runs read from stdin (the JSON list the GitHub API returns for one name) are one run with the expected
conclusion, title and summary text. The fixture workflow calls it after the ``report`` job's check step."""

from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--conclusion', required=True)
    parser.add_argument('--title', required=True)
    parser.add_argument('--contains', action='append', default=[])
    args = parser.parse_args(argv)
    runs = json.load(sys.stdin)
    problems: list[str] = []
    if len(runs) != 1:
        problems.append(f'expected one check run, found {len(runs)}')
    else:
        run = runs[0]
        output = run.get('output') or {}
        if run.get('conclusion') != args.conclusion:
            problems.append(f'conclusion {run.get("conclusion")!r}, expected {args.conclusion!r}')
        if (output.get('title') or '').strip() != args.title:
            problems.append(f'title {output.get("title")!r}, expected {args.title!r}')
        problems += [f'the summary lacks {text!r}' for text in args.contains if text not in (output.get('summary') or '')]
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
