"""Shared plumbing for the action's scripts: workflow commands, step outputs, the out dir and the exit codes."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn

#: The exit codes of ``plurai ci run``. The action itself only ever produces REFUSED.
OK = 0
POLICIES_FAILED = 1
UPGRADE_REQUIRED = 10
NOT_MEASURED = 20
REFUSED = 65
CREDENTIALS = 77

#: ``plurai ci run`` opens its comment body with this line; the action finds its sticky comment by it.
COMMENT_MARKER = '<!-- plurai-simulation-report -->'

#: GitHub rejects a comment body over this many characters and a job summary over this many bytes.
COMMENT_LIMIT_CHARS = 65_536
SUMMARY_LIMIT_BYTES = 1024 * 1024

#: Names that carry a credential for GitHub or for the runner's services, or let a process write into later steps. No
#: process that runs project code (the agent, the project interpreter's probes, the installer) receives them.
STRIPPED = frozenset(
    {
        'GITHUB_TOKEN',
        'GH_TOKEN',
        'GH_ENTERPRISE_TOKEN',
        'GITHUB_ENTERPRISE_TOKEN',
        'ACTIONS_RUNTIME_TOKEN',
        'ACTIONS_RUNTIME_URL',
        'ACTIONS_RESULTS_URL',
        'ACTIONS_CACHE_URL',
        'ACTIONS_ID_TOKEN_REQUEST_TOKEN',
        'ACTIONS_ID_TOKEN_REQUEST_URL',
        'GITHUB_OUTPUT',
        'GITHUB_ENV',
        'GITHUB_PATH',
        'GITHUB_STATE',
        'GITHUB_STEP_SUMMARY',
        # The variables the action's own steps set (the agent keeps any other `SA_*` or `INPUT_*` name it declares).
        'INPUT_CHECK_NAME',
        'INPUT_INCLUDE_CONTENT',
        'INPUT_JOB',
        'INPUT_PR_COMMENT',
        'INPUT_RETENTION_DAYS',
        'INPUT_SELECTION',
        'INPUT_TEST_REPORT',
        'INPUT_URL',
        'SA_ACTION_REF',
        'SA_BASE',
        'SA_BASE_STRATEGY',
        'SA_BIN_DIR',
        'SA_CHECK_NAME',
        'SA_COMMENT_MODE',
        'SA_DEFAULT_BRANCH',
        'SA_GIT',
        'SA_HEAD_SHA',
        'SA_INCLUDE_CONTENT_SETTING',
        'SA_FETCH_OUTCOME',
        'SA_INSTALL_OUTCOME',
        'SA_JOB_INDEX',
        'SA_JOB_TOTAL',
        'SA_MAIN_BRANCH',
        'SA_PLAN_OUTCOME',
        'SA_MAIN_RUN',
        'SA_PR_BASE_SHA',
        'SA_PR_HEAD_SHA',
        'SA_PR_NUMBER',
        'SA_PYTHON',
        'SA_RELEASE_FILE',
        'SA_REPO',
        'SA_REPORT_DIR',
        'SA_REPORT_MODE',
        'SA_RESULT',
        'SA_RUN_OUTCOME',
        'SA_SELECTION',
        'SA_TOKEN',
        'SA_URL',
        'SA_UV',
        'SA_VISIBILITY',
        'SA_WORKDIR',
        'SA_WORKFLOW_KEY',
    }
)

SELECTIONS = ('smoke', 'full')
VISIBILITIES = ('public', 'private', 'internal')
HEADING_PREFIX = '## Plurai simulation · '


class Refusal(Exception):
    """The inputs or the event cannot be run; ends the step with exit 65 and a summary heading naming the cause."""

    code = REFUSED
    verdict = 'refused'

    def __init__(self, reason: str, fix: str = '') -> None:
        super().__init__(reason)
        self.reason = reason
        self.fix = fix


def _escape_command_data(text: str) -> str:
    return text.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def notice(message: str) -> None:
    print(f'::notice::{_escape_command_data(message)}')


def warning(message: str) -> None:
    print(f'::warning::{_escape_command_data(message)}')


def error(message: str) -> None:
    print(message, file=sys.stderr)
    print(f'::error::{_escape_command_data(message)}')


def set_output(name: str, value: str, env: Mapping[str, str] | None = None) -> None:
    """Append ``name=value`` to ``$GITHUB_OUTPUT``; a multi-line value gets a heredoc delimiter that its text does not contain."""
    path = (env or os.environ).get('GITHUB_OUTPUT')
    if not path:
        return
    with open(path, 'a', encoding='utf-8') as handle:
        if '\n' in value:
            delimiter = 'PLURAI_EOF'
            while delimiter in value:
                delimiter += '_'
            handle.write(f'{name}<<{delimiter}\n{value}\n{delimiter}\n')
        else:
            handle.write(f'{name}={value}\n')


def out_dir(env: Mapping[str, str]) -> Path:
    """The directory ``plurai ci run`` writes into (``PLURAI_CI_OUT_DIR``); created on first use."""
    configured = env.get('PLURAI_CI_OUT_DIR')
    path = Path(configured) if configured else Path(env.get('RUNNER_TEMP') or tempfile.gettempdir()) / 'plurai-ci'
    path.mkdir(parents=True, exist_ok=True)
    return path


def add_note(directory: Path, text: str) -> None:
    """Record a remark of the action itself (a stale managed block, no main run fetched) for the final summary."""
    with open(directory / 'notes.md', 'a', encoding='utf-8') as handle:
        handle.write(f'- {text}\n')


def summary_note(env: Mapping[str, str], text: str) -> None:
    """A remark of a job that has no out dir to collect notes in (the ``report`` job): a line of its step summary."""
    path = env.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(f'- {text}\n')


def read_notes(directory: Path) -> list[str]:
    path = directory / 'notes.md'
    if not path.exists():
        return []
    return [line[2:] for line in path.read_text(encoding='utf-8').splitlines() if line.startswith('- ')]


def read_event(env: Mapping[str, str]) -> dict[str, Any]:
    path = env.get('GITHUB_EVENT_PATH')
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding='utf-8') as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def workflow_key(env: Mapping[str, str], job: str | None = None) -> str:
    """A stable name for the calling job: its workflow file's stem (else the workflow's name) and its job id, as a slug.

    Artifact names and the sticky comment's marker carry it, so two agents' workflows, or two jobs of one workflow,
    never overwrite each other. ``job`` names another job of the same workflow, for the ``report`` job that speaks for the
    ``simulate`` job.
    """
    ref = env.get('GITHUB_WORKFLOW_REF', '')
    match = re.search(r'/\.github/workflows/([^@]+)@', ref)
    raw = Path(match.group(1)).stem if match else env.get('GITHUB_WORKFLOW', '')
    parts = [raw, env.get('GITHUB_JOB', '') if job is None else job]
    return re.sub(r'[^a-z0-9._-]+', '-', '-'.join(p for p in parts if p).lower()).strip('-') or 'workflow'


def resolve_include_content(setting: str, visibility: str) -> bool:
    """An explicit setting wins; ``auto`` is on only for ``private`` (``internal`` and unknown count as public)."""
    if setting == 'auto':
        return visibility == 'private'
    return setting == 'true'


def is_under(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """Whether ``path`` lies inside ``root``, by its spelling and by where it leads: a venv's ``python`` is a link to a
    system interpreter, and it is the link's location that makes Python read the venv beside it."""
    root_real = Path(root).resolve()
    spelled = Path(os.path.abspath(path))
    return any(candidate == root_real or root_real in candidate.parents for candidate in (spelled, spelled.resolve()))


def refusal_documents(reason: str, fix: str, verdict: str = 'refused') -> tuple[str, str]:
    """The summary and comment bodies of a run the action stopped before ``plurai ci run`` started."""
    heading = f'{HEADING_PREFIX}⛔ {verdict}: {reason}'
    body = f'{heading}\n\n{fix}\n' if fix else f'{heading}\n'
    return body, f'{COMMENT_MARKER}\n{body}'


def refuse(directory: Path, refusal: Refusal) -> NoReturn:
    """Write the refusal's documents, print it and end the step with its exit code."""
    summary, comment = refusal_documents(refusal.reason, refusal.fix, refusal.verdict)
    (directory / 'summary.md').write_text(summary, encoding='utf-8')
    (directory / 'comment.md').write_text(comment, encoding='utf-8')
    error(f'{refusal.reason}. {refusal.fix}'.strip())
    sys.exit(refusal.code)
