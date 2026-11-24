"""Post or edit the sticky PR comment: one per PR and workflow, found by its marker and edited in place.

Runs in the ``report`` job, which never runs the agent's code, from the report artifact the ``simulate`` job uploaded.
That artifact is untrusted text: only the one file ``comment-final.md`` is read, as a regular file, up to a size cap,
and the result is cut to the comment limit. Never fails the job: a token that cannot write leaves a note in the job
summary, and any other failure (an API error, a transport error, a reply of an unexpected shape) is a warning. A report
that is not there at all never replaces a comment that exists: the comment of an earlier run stays, with a note.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import common
import summary
from common import COMMENT_LIMIT_CHARS, COMMENT_MARKER
from github import ApiError, GitHubApi

#: The author of a comment made with the job's token.
AUTHOR = 'github-actions[bot]'
#: The report file is read up to this many bytes; the comment limit is far below it, so a cut never splits what is posted.
MAX_REPORT_BYTES = 4 * COMMENT_LIMIT_CHARS
REPORT_FILE = 'comment-final.md'
SHA_MARKER_PREFIX = '<!-- plurai-simulation-report:sha='
CUT_POINTER = '\n\n_Cut to fit the comment limit; the full text is in the report artifact (`report.md`)._\n'


def key_marker(key: str) -> str:
    return f'<!-- plurai-simulation-report:workflow={key} -->'


def sha_marker(sha: str) -> str:
    return f'{SHA_MARKER_PREFIX}{sha} -->'


def compose(body: str, key: str, sha: str = '') -> str:
    """The body with the shared marker first, this workflow's marker second, the run's head sha third, cut to GitHub's limit."""
    text = body.removeprefix(COMMENT_MARKER).lstrip('\n')
    markers = f'{COMMENT_MARKER}\n{key_marker(key)}\n' + (f'{sha_marker(sha)}\n' if sha else '')
    document = f'{markers}{text}'
    if len(document) <= COMMENT_LIMIT_CHARS:
        return document
    return document[: COMMENT_LIMIT_CHARS - len(CUT_POINTER)] + CUT_POINTER


def _head(body: str | None) -> list[str]:
    """The two marker lines a comment of ours opens with; text further down is the report's own and proves nothing."""
    return (body or '').splitlines()[:2]


def _sha_of(body: str | None) -> str:
    """The head sha a comment of ours was written for, from its third line; empty when it has none."""
    lines = (body or '').splitlines()
    if len(lines) > 2 and lines[2].startswith(SHA_MARKER_PREFIX) and lines[2].endswith(' -->'):
        return lines[2][len(SHA_MARKER_PREFIX) : -len(' -->')]
    return ''


def current_head(api: GitHubApi, repository: str, number: str) -> str:
    """The PR's head sha now; empty when it cannot be read."""
    try:
        return str(api.get(f'/repos/{repository}/pulls/{number}')['head']['sha'])
    except (ApiError, KeyError, TypeError):
        return ''


def find_comment(api: GitHubApi, repository: str, number: str, key: str) -> dict[str, Any] | None:
    """This workflow's comment on the PR, found by its marker and its author."""
    marker = key_marker(key)
    comments = [c for c in api.pages(f'/repos/{repository}/issues/{number}/comments', key='') if isinstance(c, dict)]
    return next((c for c in comments if (c.get('user') or {}).get('login') == AUTHOR and marker in _head(c.get('body'))), None)


def upsert(
    api: GitHubApi,
    repository: str,
    number: str,
    body: str,
    key: str,
    guard: tuple[str, str, str] | None = None,
    *,
    only_new: bool = False,
) -> str:
    """Edit this workflow's comment on the PR, else post one; returns its URL, or '' when nothing was written.

    ``guard`` is ``(this run's head sha, the PR's head sha now, the simulate job's result)``. A cancelled run does not
    replace a comment written for another head unless it is known to be the PR's head. ``only_new`` posts when there is
    no comment yet and leaves an existing one as it is.
    """
    found = find_comment(api, repository, number, key)
    if found and only_new:
        return ''
    if found and guard:
        sha, pr_head, result = guard
        owner = _sha_of(found.get('body'))
        if result == 'cancelled' and owner and sha and owner != sha and pr_head != sha:
            common.warning(f'the comment is left alone: it was written for {owner[:12]}, and this cancelled run is {sha[:12]}')
            return ''
    if found:
        return api.call('PATCH', f'/repos/{repository}/issues/comments/{found["id"]}', {'body': body})['html_url']
    return api.call('POST', f'/repos/{repository}/issues/{number}/comments', {'body': body})['html_url']


def read_report(directory: Path) -> str | None:
    """The uploaded comment body, or None when the artifact has none."""
    path = directory / REPORT_FILE
    if path.is_symlink() or not path.is_file():
        return None
    with open(path, 'rb') as handle:
        return handle.read(MAX_REPORT_BYTES).decode('utf-8', errors='replace')


def missing_report(result: str) -> str:
    """The body for a run whose report artifact is not there (the job was cancelled, crashed or never uploaded one)."""
    heading, line = summary.missing_artifact_heading(result)
    return f'{COMMENT_MARKER}\n{heading}\n\n{line}\n'


def post(env: Mapping[str, str]) -> str:
    """The comment's URL, or '' when nothing was posted."""
    if env.get('SA_COMMENT_MODE') != 'on':
        return ''
    key = env['SA_WORKFLOW_KEY']
    sha = env.get('SA_HEAD_SHA', '')
    result = env.get('SA_RESULT', '')
    report = read_report(Path(env['SA_REPORT_DIR']))
    body = compose(missing_report(result) if report is None else report, key, sha)
    repository, number = env['GITHUB_REPOSITORY'], env['SA_PR_NUMBER']
    api = GitHubApi.from_env(env)
    pr_head = current_head(api, repository, number) if sha else ''
    if sha and pr_head and pr_head != sha:
        common.notice(f'the comment is left alone: this run ({sha[:12]}) is no longer the head of the PR ({pr_head[:12]})')
        common.summary_note(env, f'PR comment skipped: this run is no longer the head of the PR ({pr_head[:12]}).')
        return ''
    try:
        url = upsert(api, repository, number, body, key, (sha, pr_head, result), only_new=report is None)
        if report is None and not url:
            common.summary_note(env, 'PR comment left as it is: the simulate job left no report artifact to replace it with.')
        return url
    except ApiError as error:
        if error.status in (403, 404):
            common.summary_note(
                env, 'PR comment skipped: the token cannot write to the PR; the `report` job needs `pull-requests: write`.'
            )
        else:
            common.summary_note(env, f'PR comment not posted ({error}).')
            common.warning(f'the PR comment was not posted: {error}')
        return ''
    except (KeyError, TypeError, ValueError) as error:
        common.summary_note(env, f'PR comment not posted (GitHub replied in a shape the action does not know: {error!r}).')
        common.warning(f'the PR comment was not posted: GitHub replied in a shape the action does not know ({error!r})')
        return ''


def main(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    try:
        url = post(env)
    except Exception as error:  # the comment is a courtesy: whatever goes wrong is a warning, never a red job
        common.warning(f'the PR comment was not posted: {type(error).__name__}: {error}')
        url = ''
    try:
        common.set_output('comment_url', url, env)
    except OSError as error:
        common.warning(f'the output `comment_url` could not be written: {error}')
    return common.OK


if __name__ == '__main__':
    sys.exit(main())
