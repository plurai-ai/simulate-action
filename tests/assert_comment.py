"""Assertions about the sticky PR comments, read from ``gh api --paginate --slurp`` output on stdin."""

from __future__ import annotations

import argparse
import json
import sys

AUTHOR = 'github-actions[bot]'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--key', required=True, help='the workflow key in the comment marker')
    parser.add_argument('--count', type=int, required=True)
    parser.add_argument('--contains', default='')
    args = parser.parse_args(argv)
    pages = json.load(sys.stdin)
    comments = [c for page in pages for c in (page if isinstance(page, list) else [page])]
    marker = f'<!-- plurai-simulation-report:workflow={args.key} -->'
    ours = [c for c in comments if c['user']['login'] == AUTHOR and marker in c['body']]
    problems = []
    if len(ours) != args.count:
        problems.append(f'{len(ours)} comments carry {marker}, expected {args.count}')
    if args.contains and ours and args.contains not in ours[-1]['body']:
        problems.append(f'the comment lacks {args.contains!r}')
    for problem in problems:
        print(f'::error::{problem}')
    if not problems:
        print(f'ok: {len(ours)} comment(s) for {args.key}')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
