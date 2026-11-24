"""Compose what the action publishes from the files ``plurai ci run`` left in the out dir.

Usage: ``summary.py summary|comment``.

- ``summary``: the job summary is the results body, then the suite diff, then the action's own notes. It is appended to
  ``$GITHUB_STEP_SUMMARY`` and kept as ``job-summary.md``. GitHub uploads a step's summary when the step ends, so it is
  written once, after the run.
- ``comment``: ``comment-final.md`` is the results comment body followed by the short suite diff, when that fits.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

import common
from common import COMMENT_LIMIT_CHARS, COMMENT_MARKER, HEADING_PREFIX, SUMMARY_LIMIT_BYTES

DETAILS = re.compile(r'<details\b.*?</details>', re.DOTALL | re.IGNORECASE)
DIFF_POINTER = '_The suite diff is in the `suite-diff.md` artifact._'
NOTES_HEADING = '### Notes from the action'


def _read(directory: Path, name: str) -> str:
    path = directory / name
    return path.read_text(encoding='utf-8') if path.exists() else ''


#: The steps that run before ``plurai ci run``, in order, with the outcome variable the workflow passes and how the
#: summary names the step.
PRE_RUN_STEPS = (
    ('SA_PLAN_OUTCOME', 'the plan'),
    ('SA_FETCH_OUTCOME', "the fetch of main's run"),
    ('SA_INSTALL_OUTCOME', 'the SDK install'),
)
STOPPED_OUTCOMES = ('failure', 'cancelled')


def failed_pre_run_step(env: Mapping[str, str]) -> str:
    """The name of the first step before the run that failed or was cancelled; empty when none reports it."""
    return next((label for name, label in PRE_RUN_STEPS if env.get(name) in STOPPED_OUTCOMES), '')


def stopped_heading(outcome: str, failed_step: str = '') -> tuple[str, str]:
    """The heading and the line under it for a run that wrote no summary of its own.

    ``outcome`` is the run step's outcome: ``cancelled``, ``skipped`` (a step before it stopped the job), ``success`` (it
    ended green and left no report) or ``failure`` (it ended without one).
    """
    if outcome == 'cancelled':
        return (
            f'{HEADING_PREFIX}⛔ not measured: the run was cancelled',
            'The job was cancelled before `plurai ci run` wrote its report.',
        )
    if outcome == 'skipped':
        what = (
            f'The step that failed was {failed_step}, before `plurai ci run` started.'
            if failed_step
            else 'A step failed before `plurai ci run` started.'
        )
        return f'{HEADING_PREFIX}⛔ not measured: the run did not start', f"{what} See the job log for that step's own message."
    if outcome == 'success':
        return (
            f'{HEADING_PREFIX}⛔ no report: `plurai ci run` finished but left no report',
            "The report files are missing from the run's out dir. See the job log.",
        )
    return (
        f'{HEADING_PREFIX}⛔ no report: `plurai ci run` ended without writing one',
        "See the job log for the command's own message.",
    )


def missing_artifact_heading(result: str) -> tuple[str, str]:
    """The heading and the line for the report job when the simulate job's report artifact is not there at all."""
    if result == 'cancelled':
        return (
            f'{HEADING_PREFIX}⛔ not measured: the run was cancelled',
            'The job was cancelled before it uploaded its report.',
        )
    return (
        f'{HEADING_PREFIX}⛔ no report: the simulate job left no report artifact',
        f'The job result was `{result or "unknown"}`. See the job log of the simulate job for what stopped it.',
    )


def results_body(directory: Path, outcome: str, failed_step: str = '') -> str:
    body = _read(directory, 'summary.md')
    if body.strip():
        return body.rstrip('\n') + '\n'
    heading, line = stopped_heading(outcome, failed_step)
    return f'{heading}\n\n{line}\n'


def fit_diff(diff: str, room_bytes: int) -> str:
    """The suite diff cut to ``room_bytes``: bodies first, then the whole diff for a pointer."""
    if len(diff.encode()) <= room_bytes:
        return diff
    headings_only = DETAILS.sub('', diff).rstrip('\n') + f'\n\n{DIFF_POINTER}\n'
    if len(headings_only.encode()) <= room_bytes:
        return headings_only
    return DIFF_POINTER + '\n'


def compose_summary(directory: Path, outcome: str, failed_step: str = '') -> str:
    results = results_body(directory, outcome, failed_step)
    diff = _read(directory, 'suite-diff.md').strip('\n')
    notes = common.read_notes(directory)
    notes_block = f'\n{NOTES_HEADING}\n' + ''.join(f'- {note}\n' for note in notes) if notes else ''
    parts = [results]
    if diff:
        room = SUMMARY_LIMIT_BYTES - len(results.encode()) - len(notes_block.encode()) - 4
        parts.append('\n' + fit_diff(diff, room) + '\n')
    document = ''.join(parts).rstrip('\n') + '\n' + notes_block
    return document


def compose_comment(directory: Path, outcome: str, failed_step: str = '') -> str:
    body = _read(directory, 'comment.md').strip('\n')
    if not body.strip():
        heading, line = stopped_heading(outcome, failed_step)
        body = f'{COMMENT_MARKER}\n{heading}\n\n{line}'
    if not body.startswith(COMMENT_MARKER):
        body = f'{COMMENT_MARKER}\n{body}'
    diff = _read(directory, 'suite-diff-comment.md').strip('\n')
    if not diff:
        return body + '\n'
    combined = f'{body}\n\n{diff}\n'
    if len(combined) <= COMMENT_LIMIT_CHARS:
        return combined
    return f'{body}\n\n{DIFF_POINTER}\n'


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    directory = common.out_dir(env)
    outcome = env.get('SA_RUN_OUTCOME', '')
    failed_step = failed_pre_run_step(env)
    mode = argv[0] if argv else 'summary'
    if mode == 'summary':
        document = compose_summary(directory, outcome, failed_step)
        (directory / 'job-summary.md').write_text(document, encoding='utf-8')
        target = env.get('GITHUB_STEP_SUMMARY')
        if target:
            with open(target, 'a', encoding='utf-8') as handle:
                handle.write(document)
    elif mode == 'comment':
        (directory / 'comment-final.md').write_text(compose_comment(directory, outcome, failed_step), encoding='utf-8')
    else:
        common.error(f'unknown mode `{mode}`')
        return common.REFUSED
    return common.OK


if __name__ == '__main__':
    sys.exit(main())
