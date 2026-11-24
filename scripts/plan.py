"""Validate the inputs, map the event to a selection and decide what the later steps do.

Reads the action inputs from ``INPUT_*`` and the event from ``GITHUB_EVENT_*``; writes step outputs. A refusal ends
with exit 65 after writing the summary and comment bodies a refused run shows.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Mapping
from typing import Any

import common
from common import Refusal, resolve_include_content

#: ``pull_request``, ``merge_group`` and a ``workflow_dispatch`` without a ``selection`` run the smoke list; ``push`` and
#: ``schedule`` run the full suite.
SELECTION_BY_EVENT = {
    'pull_request': 'smoke',
    'merge_group': 'smoke',
    'push': 'full',
    'schedule': 'full',
    'workflow_dispatch': 'smoke',
}
CONTENT_SETTINGS = ('auto', 'true', 'false')
RETENTION_MIN, RETENTION_MAX = 1, 90
NULL_SHA = '0' * 40


def choose_selection(event_name: str, requested: str) -> str:
    if event_name not in SELECTION_BY_EVENT:
        raise Refusal(
            f'event `{event_name}` is not supported',
            'Run the action on `pull_request`, `merge_group`, `push`, `schedule` or `workflow_dispatch`. '
            "`pull_request_target` is refused on purpose: it would give the PR's code the repository secrets.",
        )
    if event_name != 'workflow_dispatch':
        if requested:
            common.warning(f'input `selection` is ignored on `{event_name}`; the action maps the event itself')
        return SELECTION_BY_EVENT[event_name]
    if not requested:
        return SELECTION_BY_EVENT[event_name]
    if requested not in common.SELECTIONS:
        raise Refusal(f'input `selection` is `{requested}`', 'Use `smoke` or `full`, or leave it empty.')
    return requested


def base_plan(event_name: str, event: Mapping[str, Any]) -> tuple[str, str]:
    """The strategy for the suite diff's base commit and the sha when the event already names it."""
    if event_name == 'merge_group':
        sha = (event.get('merge_group') or {}).get('base_sha') or ''
        return ('event', sha) if sha else ('none', '')
    if event_name == 'push':
        before = event.get('before') or ''
        return ('event', before) if before and before != NULL_SHA else ('none', '')
    if event_name == 'pull_request':
        return 'merge-base', ''
    return 'last-main-run', ''


def baseline_branch(event_name: str, event: Mapping[str, Any], default_branch: str) -> str:
    """The branch whose runs are "main" for this event: the one a pull request or a merge group lands on, else the default."""
    if event_name == 'pull_request':
        return ((event.get('pull_request') or {}).get('base') or {}).get('ref') or default_branch
    if event_name == 'merge_group':
        return str((event.get('merge_group') or {}).get('base_ref') or '').removeprefix('refs/heads/') or default_branch
    return default_branch


def default_branch_of(event_name: str, event: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """The repository's default branch: the event says it, except a ``schedule`` event (no repository in its payload),
    which GitHub only ever runs on the default branch."""
    named = (event.get('repository') or {}).get('default_branch')
    if named:
        return str(named)
    if event_name == 'schedule' and env.get('GITHUB_REF_NAME'):
        return env['GITHUB_REF_NAME']
    return 'main'


def check_job(env: Mapping[str, str]) -> None:
    """Refuse a matrix leg: every leg writes the same artifact and the same sticky comment, so the last one wins."""
    total = env.get('SA_JOB_TOTAL', '').strip()
    if total.isdigit() and int(total) > 1:
        raise Refusal(
            f'the job is one of {total} matrix legs',
            "Every leg would overwrite the others' report artifact and PR comment. Run one `simulate` job per agent "
            'instead: a separate job (and workflow file) for each, without `strategy.matrix`.',
        )


def check_interpreter(env: Mapping[str, str]) -> None:
    """Refuse an interpreter that lives in the checkout: the later steps run it while holding the job token, and a project
    can plant a ``pyvenv.cfg`` or a ``.pth`` file beside it."""
    workspace = env.get('GITHUB_WORKSPACE')
    if workspace and common.is_under(sys.executable, workspace):
        raise Refusal(
            f'the interpreter that runs the action is inside the workspace ({sys.executable})',
            'Put `python3` from `actions/setup-python` or the runner image first on PATH, and activate the project '
            'environment only after this action (or leave it to the action, which finds `.venv` itself).',
        )


def trusted_tools(env: Mapping[str, str]) -> dict[str, str]:
    """Absolute paths of the interpreter and tools the later steps run, resolved before any step executes project code.

    A step after the agent's run could otherwise find a different ``python3`` through a ``PATH`` the agent changed. An
    empty value means the tool is absent. The project's own interpreter and ``plurai`` are the exception by design: they
    come from the project environment.
    """
    path = env.get('PATH', '')
    return {
        'python': sys.executable,
        'uv': shutil.which('uv', path=path) or '',
        'git': shutil.which('git', path=path) or '',
    }


def plan(env: Mapping[str, str]) -> dict[str, str]:
    check_interpreter(env)
    check_job(env)
    event_name = env.get('GITHUB_EVENT_NAME', '')
    event = common.read_event(env)
    selection = choose_selection(event_name, env.get('INPUT_SELECTION', '').strip())

    setting = (env.get('INPUT_INCLUDE_CONTENT', '') or 'auto').strip().lower()
    if setting not in CONTENT_SETTINGS:
        raise Refusal(f'input `include_content` is `{setting}`', 'Use `auto`, `true` or `false`.')

    days_text = (env.get('INPUT_RETENTION_DAYS', '') or '7').strip()
    if not re.fullmatch(r'\d+', days_text) or not RETENTION_MIN <= int(days_text) <= RETENTION_MAX:
        raise Refusal(
            f'input `retention_days` is `{days_text}`', f'Use a whole number of days from {RETENTION_MIN} to {RETENTION_MAX}.'
        )

    visibility = (event.get('repository') or {}).get('visibility') or ''
    if visibility not in common.VISIBILITIES:
        visibility = ''

    strategy, base = base_plan(event_name, event)
    pull_request = event.get('pull_request') or {}
    default_branch = default_branch_of(event_name, event, env)
    directory = common.out_dir(env)

    return {
        'selection': selection,
        'visibility': visibility,
        'include_content_setting': setting,
        'include_content': 'true' if resolve_include_content(setting, visibility) else 'false',
        'retention_days': days_text,
        'base_strategy': strategy,
        'base': base,
        'pr_base_sha': (pull_request.get('base') or {}).get('sha') or '',
        'pr_head_sha': (pull_request.get('head') or {}).get('sha') or '',
        'default_branch': default_branch,
        'main_branch': baseline_branch(event_name, event, default_branch),
        'workflow_key': common.workflow_key(env),
        'out_dir': str(directory),
        **trusted_tools(env),
    }


def main(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    try:
        outputs = plan(env)
    except Refusal as refusal:
        directory = common.out_dir(env)
        refused = {
            'out_dir': str(directory),
            'workflow_key': common.workflow_key(env),
            'include_content': 'false',
            'retention_days': '7',
            **trusted_tools(env),
        }
        for name, value in refused.items():
            common.set_output(name, value, env)
        common.refuse(directory, refusal)
    for name, value in outputs.items():
        common.set_output(name, value, env)
    return common.OK


if __name__ == '__main__':
    sys.exit(main())
