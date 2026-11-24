"""First step of the ``report`` job: decide whether the sticky PR comment and the test report are published, and name the
report to publish from.

The ``report`` job runs apart from the ``simulate`` job, which runs the agent's code. This step reads only the event and
the action's inputs; its outputs name the artifact the ``simulate`` job uploaded, the comment's marker key and the check run
that shows the report.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Any

import common

DEPENDABOT_ACTOR = 'dependabot[bot]'


CHECK_PREFIX = 'Plurai simulation'


def write_restriction(event_name: str, event: Mapping[str, Any], repository: str, actor: str) -> str:
    """Why the job token cannot write on this event, as the tail of a note; empty when it can."""
    if event_name != 'pull_request':
        return ''
    head_repo = ((event.get('pull_request') or {}).get('head') or {}).get('repo') or {}
    if head_repo.get('full_name') and head_repo['full_name'] != repository:
        return 'the PR comes from a fork, so the token cannot write to it.'
    if actor == DEPENDABOT_ACTOR:
        return 'Dependabot runs get a read-only token.'
    return ''


def publish_plan(
    event_name: str,
    event: Mapping[str, Any],
    repository: str,
    actor: str,
    *,
    comment: bool,
    report: bool,
    result: str,
) -> tuple[str, str, str]:
    """``(comment_mode, report_mode, note)``: each mode is ``on``, ``off`` (nothing to say) or ``skip`` (the note says why).

    The comment is for a pull request only. The test report follows every event that ran the ``simulate`` job. Both need a
    token that can write, which a fork PR and a Dependabot PR do not get.
    """
    if result == 'skipped':
        return 'off', 'off', ''
    wants = {'PR comment': comment and event_name == 'pull_request', 'test report': report}
    reason = write_restriction(event_name, event, repository, actor)
    modes = {name: ('skip' if reason else 'on') if wanted else 'off' for name, wanted in wants.items()}
    skipped = [name for name, mode in modes.items() if mode == 'skip']
    note = f'{" and ".join(skipped)} skipped: {reason}' if skipped else ''
    return modes['PR comment'], modes['test report'], note


def check_name(workflow: str) -> str:
    """The name of the check run that shows the report: the workflow's name with ``results`` after the template's prefix, so it
    never equals the ``simulate`` check that branch protection requires."""
    if workflow.startswith(CHECK_PREFIX):
        return f'{CHECK_PREFIX} results{workflow[len(CHECK_PREFIX) :]}'
    return f'{CHECK_PREFIX} results ({workflow})'


def main(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    job = env.get('INPUT_JOB', '').strip()
    if not job:
        common.error('input `job` is empty: name the id of the job that runs the simulate-action step (`simulate` by default)')
        return common.REFUSED
    event = common.read_event(env)
    comment_mode, report_mode, note = publish_plan(
        env.get('GITHUB_EVENT_NAME', ''),
        event,
        env.get('GITHUB_REPOSITORY', ''),
        env.get('GITHUB_ACTOR', ''),
        comment=env.get('INPUT_PR_COMMENT', 'true').strip().lower() == 'true',
        report=env.get('INPUT_TEST_REPORT', 'true').strip().lower() == 'true',
        result=env.get('SA_RESULT', ''),
    )
    if note:
        common.summary_note(env, note)
    key = common.workflow_key(env, job)
    outputs = {
        'comment_mode': comment_mode,
        'report_mode': report_mode,
        'check_name': env.get('INPUT_CHECK_NAME', '').strip() or check_name(env.get('GITHUB_WORKFLOW', '').strip()),
        'pr_number': str(((event.get('pull_request') or {}).get('number')) or ''),
        'head_sha': ((event.get('pull_request') or {}).get('head') or {}).get('sha') or '',
        'workflow_key': key,
        'artifact': f'plurai-{key}-report',
    }
    for name, value in outputs.items():
        common.set_output(name, value, env)
    return common.OK


if __name__ == '__main__':
    sys.exit(main())
