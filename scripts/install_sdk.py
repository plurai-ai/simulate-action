"""Make sure the project's interpreter holds a ``plurai-simulation-sdk`` the action can run, installing the pin when it has none.

The pin and its source (PyPI) live in ``release.json``, one place per action release. The interpreter is the environment
the caller's own install steps created: the nearest ``.venv`` above ``workdir``, else ``$VIRTUAL_ENV``, else
``python3``. Only that interpreter is asked; lockfiles are not read, so a lock that names another version is caught by
the SDK's own check at run time.

The accepted range is the pin's ``major.minor`` at or above the pin. An SDK in the range is used as is. None installed: the
pin is installed. Any other version is refused, so the run never mixes SDKs that the action was not tested with. This module
runs on the runner's ``python3`` and imports nothing outside the standard library, so the version order is written here.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import common
from common import Refusal

PACKAGE = 'plurai-simulation-sdk'
PYTHON_FLOOR = (3, 12)
SOURCE = 'pypi'
DEFAULT_RELEASE_FILE = Path(__file__).resolve().parent.parent / 'release.json'
# PEP 440 spellings plus the dash forms ``1.1.0-dev.0`` and ``1.1.0-rc.1``; a local ``+tag`` is ignored.
VERSION = re.compile(
    r'^(?P<release>\d+(?:\.\d+)*)(?:[-_.]?(?P<pre>a|b|rc)[-_.]?(?P<pre_n>\d*))?'
    r'(?:[-_.]?post[-_.]?(?P<post>\d*))?(?:[-_.]?dev[-_.]?(?P<dev>\d*))?(?:\+[\w.]+)?$'
)
PRE_RANK = {'a': 0, 'b': 1, 'rc': 2}
BELOW_ALL_PRE = (-1, 0)
ABOVE_ALL_PRE = (3, 0)


@dataclass(frozen=True)
class Version:
    text: str
    minor: tuple[int, int]
    key: tuple[object, ...]


def parse_version(text: str) -> Version:
    """The ``major.minor`` and the sort key of ``text``, ordered as PEP 440 orders it: dev < a < b < rc < release < post."""
    match = VERSION.match(text.strip().lower())
    if not match:
        raise ValueError(f'{text!r} is not a version')
    release = [int(part) for part in match['release'].split('.')]
    minor = (release[0], release[1] if len(release) > 1 else 0)
    while release and release[-1] == 0:
        release.pop()
    pre = (PRE_RANK[match['pre']], int(match['pre_n'] or 0)) if match['pre'] else None
    post = int(match['post'] or 0) if match['post'] is not None else None
    dev = int(match['dev'] or 0) if match['dev'] is not None else None
    if pre is None and post is None and dev is not None:
        pre = BELOW_ALL_PRE
    elif pre is None:
        pre = ABOVE_ALL_PRE
    return Version(text, minor, (tuple(release), pre, -1 if post is None else post, float('inf') if dev is None else dev))


def accepts(pin: Version, candidate: Version) -> bool:
    return candidate.minor == pin.minor and candidate.key >= pin.key


def range_text(pin: Version) -> str:
    return f'>={pin.text},<{pin.minor[0]}.{pin.minor[1] + 1}.0'


def load_pin(env: Mapping[str, str]) -> dict[str, Any]:
    path = Path(env.get('SA_RELEASE_FILE') or DEFAULT_RELEASE_FILE)
    return json.loads(path.read_text(encoding='utf-8'))['sdk']


def ancestors(workdir: Path, workspace: Path) -> list[Path]:
    """``workdir`` and each directory above it, up to and including the workspace root."""
    workdir, workspace = workdir.resolve(), workspace.resolve()
    chain = [workdir]
    for parent in workdir.parents:
        chain.append(parent)
        if parent == workspace:
            break
    return chain if workspace in chain else [workdir]


def find_python(env: Mapping[str, str], workdir: Path, workspace: Path) -> Path:
    for directory in ancestors(workdir, workspace):
        candidate = directory / '.venv' / 'bin' / 'python'
        if candidate.exists():
            return candidate
    if env.get('VIRTUAL_ENV'):
        candidate = Path(env['VIRTUAL_ENV']) / 'bin' / 'python'
        if candidate.exists():
            return candidate
    found = shutil.which('python3', path=env.get('PATH', ''))
    if found:
        return Path(found)
    raise Refusal(
        'no Python interpreter was found for the project',
        'Create the project environment in your own install steps before this action (for example `uv sync`).',
    )


def scrubbed(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` without any name of the shared deny-list.

    The project's interpreter and the installer run code the pull request controls (site customisation, installed
    packages, build backends), so neither may see a credential or a step file.
    """
    return {name: value for name, value in env.items() if name not in common.STRIPPED}


def query(python: Path, code: str, env: Mapping[str, str]) -> str | None:
    result = subprocess.run([str(python), '-I', '-c', code], env=scrubbed(env), capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def check_python_version(python: Path, env: Mapping[str, str]) -> None:
    version = query(python, 'import platform; print(platform.python_version())', env) or ''
    match = re.match(r'(\d+)\.(\d+)', version)
    if not match or (int(match.group(1)), int(match.group(2))) < PYTHON_FLOOR:
        raise Refusal(
            f'the project interpreter is Python {version or "of unknown version"}; the SDK needs 3.12 or newer',
            f'Use Python 3.12 or newer in your install steps. Interpreter: `{python}`.',
        )


def installed_version(python: Path, env: Mapping[str, str]) -> str | None:
    return query(python, f'import importlib.metadata as m; print(m.version("{PACKAGE}"))', env)


def check_range(present: str, pin: Version) -> None:
    """Refuse an installed SDK that is outside the range the action was tested with."""
    try:
        found = parse_version(present)
    except ValueError:
        found = None
    if found is None or not accepts(pin, found):
        raise Refusal(
            f'the project environment holds `{PACKAGE}` {present}; this release of the action accepts {range_text(pin)}',
            f'Set `{PACKAGE}` in the project dependencies and lockfile to a version in that range and install again '
            '(the plugin writes it for uv projects).',
        )


def install_command(python: Path, requirement: str, uv: str | None) -> list[str]:
    if uv:
        return [uv, 'pip', 'install', '--python', str(python), requirement]
    return [str(python), '-m', 'pip', 'install', requirement]


def run(env: Mapping[str, str]) -> dict[str, str]:
    pin = load_pin(env)
    if pin.get('source') != SOURCE:
        raise Refusal(
            f'`release.json` names the SDK source `{pin.get("source")}`',
            f'This action installs `{PACKAGE}` from PyPI only; set `sdk.source` to `{SOURCE}`.',
        )
    skip = env.get('PLURAI_ACTION_SKIP_INSTALL') == '1'
    uv = env.get('SA_UV') or None
    workspace = Path(env.get('GITHUB_WORKSPACE') or '.')
    workdir = workspace / (env.get('SA_WORKDIR') or '.')
    python = find_python(env, workdir, workspace)
    check_python_version(python, env)
    pin_version = pin['version']
    parsed_pin = parse_version(pin_version)
    present = installed_version(python, env)
    if present:
        check_range(present, parsed_pin)

    if skip:
        state = 'skipped'
    elif present:
        state = 'already'
    else:
        state = 'installed'
        requirement = f'{PACKAGE}=={pin_version}'
        completed = subprocess.run(install_command(python, requirement, uv), env=scrubbed(env), check=False)
        if completed.returncode != 0:
            raise Refusal(
                f'installing {requirement} failed (exit {completed.returncode})', 'See the job log for the installer output.'
            )
    return {'python': str(python), 'bin_dir': str(python.parent), 'installed': state, 'sdk_version': present or pin_version}


def main(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    try:
        outputs = run(env)
    except Refusal as refusal:
        common.refuse(common.out_dir(env), refusal)
    for name, value in outputs.items():
        common.set_output(name, value, env)
    return common.OK


if __name__ == '__main__':
    sys.exit(main())
