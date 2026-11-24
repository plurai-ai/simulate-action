"""Create the check run that shows the report: one per run, named for the agent, on the commit the run tested.

Runs in the ``report`` job, which never runs the agent's code, from the report artifact the ``simulate`` job uploaded.
That artifact is untrusted text: only the one file ``report.md`` is read, as a regular file, up to a size cap. Its first
line is the heading ``## Plurai simulation · <mark> <counts>``; the counts are the check's title, the mark picks the
conclusion, and the rest of the file is the summary, cut to the Checks API limit. Never fails the job: a token that cannot
write, a fork PR, a missing report and any failure of the call leave a note or a log line and no check.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

import common
from github import ApiError, GitHubApi

REPORT_FILE = 'report.md'
#: The Checks API rejects a summary over this many characters.
SUMMARY_LIMIT_CHARS = 65_535
TITLE_LIMIT_CHARS = 255
#: The report is read up to this many bytes; the summary limit is far below it, so a cut never splits what is sent.
MAX_REPORT_BYTES = 4 * SUMMARY_LIMIT_CHARS
HEADING_PREFIX = '## '
LEAD = 'Plurai simulation · '
#: The mark that opens a report's counts line, and the conclusion it stands for. ``neutral`` is a label for "not measured":
#: GitHub counts it as success for a required check, and this check is never the required one.
CONCLUSIONS = {'✅': 'success', '❌': 'failure', '⛔': 'neutral'}


def read_report(directory: Path) -> str | None:
    """The uploaded report, or None when the run left none."""
    path = directory / REPORT_FILE
    if path.is_symlink() or not path.is_file():
        return None
    with open(path, 'rb') as handle:
        return handle.read(MAX_REPORT_BYTES).decode('utf-8', errors='replace')


def split_report(text: str) -> tuple[str, str]:
    """``(title, body)``: the heading's text without the prefix, and everything under it."""
    first, _, rest = text.lstrip('\n').partition('\n')
    title = first.removeprefix(HEADING_PREFIX).removeprefix(LEAD).strip() or 'Plurai simulation'
    if len(title) > TITLE_LIMIT_CHARS:
        title = title[: TITLE_LIMIT_CHARS - 1] + '…'
    return title, rest.strip('\n')


def conclusion_of(title: str) -> str:
    return next((value for mark, value in CONCLUSIONS.items() if title.startswith(mark)), 'neutral')


def cut(body: str, run_url: str) -> str:
    pointer = f'\n\n_Cut to fit the check run limit; the full report is in the [job summary]({run_url})._\n'
    if len(body) <= SUMMARY_LIMIT_CHARS:
        return body
    return body[: SUMMARY_LIMIT_CHARS - len(pointer)] + pointer


def run_url(env: Mapping[str, str]) -> str:
    server = env.get('GITHUB_SERVER_URL') or 'https://github.com'
    return f'{server}/{env["GITHUB_REPOSITORY"]}/actions/runs/{env.get("GITHUB_RUN_ID", "")}'


def create(env: Mapping[str, str]) -> str:
    """The check run's URL, or '' when none was created."""
    if env.get('SA_REPORT_MODE') != 'on':
        return ''
    report = read_report(Path(env['SA_REPORT_DIR']))
    if report is None:
        common.notice('no check run: the simulate job left no report.md')
        return ''
    title, body = split_report(report)
    url = run_url(env)
    payload = {
        'name': env['SA_CHECK_NAME'],
        'head_sha': env.get('SA_HEAD_SHA') or env['GITHUB_SHA'],
        'status': 'completed',
        'conclusion': conclusion_of(title),
        'details_url': url,
        'output': {'title': title, 'summary': cut(body or title, url)},
    }
    try:
        return str(GitHubApi.from_env(env).call('POST', f'/repos/{env["GITHUB_REPOSITORY"]}/check-runs', payload)['html_url'])
    except ApiError as error:
        if error.status in (403, 404):
            common.summary_note(
                env, 'Test report skipped: the token cannot create a check run; the `report` job needs `checks: write`.'
            )
        else:
            common.summary_note(env, f'Test report check not created ({error}).')
            common.warning(f'the check run was not created: {error}')
        return ''
    except (KeyError, TypeError, ValueError) as error:
        common.summary_note(
            env, f'Test report check not created (GitHub replied in a shape the action does not know: {error!r}).'
        )
        common.warning(f'the check run was not created: GitHub replied in a shape the action does not know ({error!r})')
        return ''


def main(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    try:
        url = create(env)
    except Exception as error:  # the check is a courtesy: whatever goes wrong is a warning, never a red job
        common.warning(f'the check run was not created: {type(error).__name__}: {error}')
        url = ''
    try:
        common.set_output('check_url', url, env)
    except OSError as error:
        common.warning(f'the output `check_url` could not be written: {error}')
    return common.OK


if __name__ == '__main__':
    sys.exit(main())
