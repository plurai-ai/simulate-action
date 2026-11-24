"""Replace this process with ``plurai ci run``, so the runner's cancel signals reach the command.

The command line is the contract in the README. The token goes in as ``PLURAI_TOKEN``. The URL goes in as ``PLURAI_URL``
only when the ``url`` input is set; without it the command runs against production. The ref the caller pinned the action at
goes in as ``PLURAI_ACTION_VERSION``, which the command records in ``run.json``; the job never supplies it. No GitHub
credential goes in at all: the SDK reads no GitHub token and no GitHub event (the run's page address reaches it as
``--run-url``, built here), and its child (the agent) must not inherit
one. The names the action's steps set for themselves are dropped too; any other name, including one with an ``SA_`` or
``INPUT_`` prefix that the agent declares, passes through.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping

import common
from common import STRIPPED

EXECUTABLE = 'plurai'
ACTION_VERSION_VAR = 'PLURAI_ACTION_VERSION'


def run_page_url(env: Mapping[str, str]) -> str:
    """This attempt's page on GitHub Actions, the link target of the comment's "job summary" phrases; empty without all four."""
    names = ('GITHUB_SERVER_URL', 'GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT')
    parts = [env.get(name, '').strip() for name in names]
    if not all(parts):
        return ''
    server, repository, run_id, attempt = parts
    return f'{server.rstrip("/")}/{repository}/actions/runs/{run_id}/attempts/{attempt}'


def build_argv(env: Mapping[str, str]) -> list[str]:
    argv = ['ci', 'run', '--repo', env['SA_REPO'], '--selection', env['SA_SELECTION']]
    if env.get('SA_BASE'):
        argv += ['--base', env['SA_BASE']]
    if env.get('SA_MAIN_RUN'):
        argv += ['--main-run', env['SA_MAIN_RUN']]
    if env.get('SA_VISIBILITY'):
        argv += ['--visibility', env['SA_VISIBILITY']]
    if env.get('SA_INCLUDE_CONTENT_SETTING', 'auto') != 'auto':
        argv += ['--include-content', env['SA_INCLUDE_CONTENT_SETTING']]
    if run_url := run_page_url(env):
        argv += ['--run-url', run_url]
    return argv


def build_env(env: Mapping[str, str]) -> dict[str, str]:
    set_by_action = ('PLURAI_URL', 'PLURAI_TOKEN', ACTION_VERSION_VAR)
    child = {name: value for name, value in env.items() if name not in STRIPPED and name not in set_by_action}
    if url := env.get('SA_URL', '').strip():
        child['PLURAI_URL'] = url
    if action_ref := env.get('SA_ACTION_REF', '').strip():
        child[ACTION_VERSION_VAR] = action_ref
    if env.get('SA_TOKEN'):
        child['PLURAI_TOKEN'] = env['SA_TOKEN']
    if env.get('SA_BIN_DIR'):
        child['PATH'] = os.pathsep.join([env['SA_BIN_DIR'], env.get('PATH', '')])
    return child


def main(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    child = build_env(env)
    executable = shutil.which(EXECUTABLE, path=child.get('PATH', ''))
    if executable is None:
        common.error(f'`{EXECUTABLE}` is not on PATH: the SDK install step did not put it there')
        return 127
    os.execve(executable, [EXECUTABLE, *build_argv(env)], child)  # noqa: S606 - replacing the process is the point


if __name__ == '__main__':
    sys.exit(main())
