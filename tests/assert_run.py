"""Assertions the fixture workflow makes about one run of the action against the stub ``plurai``.

Exits 1 and prints every mismatch. The checks read what the action left behind (the out dir, the downloaded artifacts)
and what the stub recorded; none of them calls a backend.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SELECTION_BY_EVENT = {
    'pull_request': 'smoke',
    'merge_group': 'smoke',
    'push': 'full',
    'schedule': 'full',
    'workflow_dispatch': 'smoke',
}
#: Triggers where the base commit is always derivable, so the flag must be on the command line.
BASE_ALWAYS = ('pull_request', 'merge_group', 'push')
CREDENTIAL_NAMES = ('GITHUB_TOKEN', 'GH_TOKEN', 'ACTIONS_RUNTIME_TOKEN', 'ACTIONS_ID_TOKEN_REQUEST_TOKEN', 'GITHUB_STEP_SUMMARY')


def check(args: argparse.Namespace) -> list[str]:
    problems: list[str] = []
    out = Path(args.out_dir)
    expected_outcome = 'success' if args.expect_exit == 0 else 'failure'
    if args.outcome != expected_outcome:
        problems.append(f'step outcome is {args.outcome}, expected {expected_outcome} for exit {args.expect_exit}')

    summary = out / 'job-summary.md'
    first = summary.read_text().splitlines()[0] if summary.exists() and summary.read_text() else ''
    if not first.startswith('## Plurai simulation') or args.heading not in first:
        problems.append(f'summary heading {first!r} lacks {args.heading!r}')

    record_path = out / 'stub-record.json'
    if args.stub:
        if not record_path.exists():
            problems.append('the stub plurai never ran')
        else:
            record = json.loads(record_path.read_text())
            problems += check_record(record, args)
    elif record_path.exists():
        problems.append('the stub plurai ran, but the action should have stopped before it')

    problems += check_artifacts(args)
    return problems


def check_record(record: dict, args: argparse.Namespace) -> list[str]:
    problems: list[str] = []
    argv = record['argv']
    if record['exit'] != args.expect_exit:
        problems.append(f'the stub exited {record["exit"]}, expected {args.expect_exit}')
    if argv[:2] != ['ci', 'run']:
        problems.append(f'argv does not start with `ci run`: {argv}')
    flags = {argv[i]: argv[i + 1] for i in range(2, len(argv) - 1, 2)}
    if flags.get('--repo') != args.repo:
        problems.append(f'--repo is {flags.get("--repo")!r}, expected {args.repo!r}')
    expected_selection = args.expect_selection or SELECTION_BY_EVENT[args.event]
    if flags.get('--selection') != expected_selection:
        problems.append(f'--selection is {flags.get("--selection")!r}, expected {expected_selection!r}')
    if args.event in BASE_ALWAYS and not flags.get('--base'):
        problems.append(f'--base is missing on {args.event}')
    for name in CREDENTIAL_NAMES:
        if name in record['env']:
            problems.append(f'{name} is in the environment of plurai ci run')
    if record['env'].get('PLURAI_URL') != (args.url or None):
        problems.append(f'PLURAI_URL is {record["env"].get("PLURAI_URL")!r}, expected {(args.url or None)!r}')
    if 'PLURAI_TOKEN' not in record['env']:
        problems.append('PLURAI_TOKEN is not in the environment of plurai ci run')
    action_version = record['env'].get('PLURAI_ACTION_VERSION', '')
    if action_version != args.expect_action_version:
        problems.append(f'PLURAI_ACTION_VERSION is {action_version!r}, expected {args.expect_action_version!r}')
    if args.expect_bin_dir:
        first = record['env'].get('PATH', '').split(':')[0]
        if first != args.expect_bin_dir:
            problems.append(
                f'the first PATH entry of plurai ci run is {first!r}, expected the project venv {args.expect_bin_dir!r}'
            )
    return problems


def check_artifacts(args: argparse.Namespace) -> list[str]:
    problems: list[str] = []
    if args.report_dir:
        names = ['report.md'] if args.expect_exit in (10, 65) else ['run.json', 'report.md', 'junit.xml']
        problems += [f'the report artifact lacks {name}' for name in names if not (Path(args.report_dir) / name).exists()]
    if args.expect_transcripts != 'skip':
        directory = Path(args.transcripts_dir)
        present = directory.exists() and any(directory.iterdir())
        if args.expect_transcripts == 'present' and not present:
            problems.append('the transcripts artifact is missing')
        if args.expect_transcripts == 'absent' and present:
            problems.append('the transcripts artifact is present but content is excluded')
    return problems


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument('--out-dir', required=True)
    p.add_argument('--outcome', required=True)
    p.add_argument('--expect-exit', type=int, required=True)
    p.add_argument('--heading', required=True)
    p.add_argument('--event', required=True)
    p.add_argument('--repo', required=True)
    p.add_argument('--url', default='')
    p.add_argument('--expect-selection', default='')
    p.add_argument('--expect-bin-dir', default='')
    # The fixture runs the action from its own checkout (`uses: ./`), where `github.action_ref` is empty.
    p.add_argument('--expect-action-version', default='')
    p.add_argument('--no-stub', dest='stub', action='store_false')
    p.add_argument('--report-dir', default='')
    p.add_argument('--transcripts-dir', default='')
    p.add_argument('--expect-transcripts', choices=('present', 'absent', 'skip'), default='skip')
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    problems = check(args)
    for problem in problems:
        print(f'::error::{problem}')
    if not problems:
        print(f'ok: exit {args.expect_exit} on {args.event}')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
