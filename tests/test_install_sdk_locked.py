"""The customer's uv install step, then install_sdk.py: a lock that names another SDK of the pin's minor is used as is.

The project is real (a ``pyproject.toml``, a ``uv.lock`` and the ``.venv`` uv builds from them) and so is the SDK distribution,
a wheel built here and served from a local folder, so nothing reaches a network. The install line is the one ``/simulation-ci``
renders for a uv project: it syncs the dev group, where the plugin adds the SDK.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import install_sdk
from conftest import REPO


def next_build(version: str) -> str:
    """The pin with its last number raised by one: a later build of the same ``major.minor``, which the range accepts."""
    return re.sub(r'\d+$', lambda match: str(int(match.group()) + 1), version)


PIN = json.loads((REPO / 'release.json').read_text())['sdk']['version']
LOCKED = next_build(PIN)
AGENT = 'agents/support'
RENDERED_UV_STEP = f'uv sync --directory {AGENT} --no-default-groups --group dev'


def build_wheel(folder: Path, version: str) -> None:
    """A metadata-only ``plurai-simulation-sdk`` wheel: install_sdk.py reads the installed version and nothing else."""
    folder.mkdir(parents=True, exist_ok=True)
    info = f'plurai_simulation_sdk-{version}.dist-info'
    files = {
        f'{info}/METADATA': f'Metadata-Version: 2.1\nName: plurai-simulation-sdk\nVersion: {version}\n',
        f'{info}/WHEEL': 'Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
    }
    with zipfile.ZipFile(folder / f'plurai_simulation_sdk-{version}-py3-none-any.whl', 'w') as wheel:
        for name, text in files.items():
            wheel.writestr(name, text)
        wheel.writestr(f'{info}/RECORD', ''.join(f'{name},,\n' for name in [*files, f'{info}/RECORD']))


def uv(command: str, cwd: Path, wheels: Path) -> None:
    """``command`` as the runner would run it, with uv held to the local wheel folder and the test's own interpreter."""
    env = {
        **os.environ,
        'UV_NO_INDEX': '1',
        'UV_FIND_LINKS': str(wheels),
        'UV_OFFLINE': '1',
        'UV_PYTHON': sys.executable,
        'UV_NO_CONFIG': '1',
    }
    env.pop('VIRTUAL_ENV', None)
    subprocess.run(command.split(), cwd=cwd, env=env, check=True, capture_output=True, text=True)


def test_the_rendered_uv_step_puts_the_locked_sdk_where_the_action_uses_it_as_is(run_script, tmp_path: Path) -> None:
    assert shutil.which('uv'), 'uv runs the customer install step; the dev workflow installs it'
    wheels = tmp_path / 'wheels'
    build_wheel(wheels, LOCKED)
    workspace = tmp_path / 'workspace'
    agent = workspace / AGENT
    (agent / 'plurai_sim').mkdir(parents=True)
    (agent / 'pyproject.toml').write_text(
        '[project]\nname = "support"\nversion = "0.0.0"\nrequires-python = ">=3.12"\ndependencies = []\n\n'
        f'[dependency-groups]\ndev = ["plurai-simulation-sdk=={LOCKED}"]\n\n[tool.uv]\npackage = false\n',
        encoding='utf-8',
    )
    uv(f'uv lock --directory {AGENT}', workspace, wheels)
    uv(RENDERED_UV_STEP, workspace, wheels)
    release = tmp_path / 'release.json'
    release.write_text(json.dumps({'sdk': {'version': PIN, 'source': 'pypi'}, 'managed_rev': 1}), encoding='utf-8')

    result = run_script(
        'install_sdk.py',
        {'GITHUB_WORKSPACE': str(workspace), 'SA_WORKDIR': f'{AGENT}/plurai_sim', 'SA_RELEASE_FILE': str(release)},
    )

    assert result.code == 0, result.stderr
    assert result.outputs['python'] == str(agent / '.venv' / 'bin' / 'python')
    assert result.outputs['installed'] == 'already'
    assert result.outputs['sdk_version'] == LOCKED


def test_the_locked_version_is_in_the_range_of_the_pin_but_is_not_the_pin():
    assert LOCKED != PIN
    assert install_sdk.accepts(install_sdk.parse_version(PIN), install_sdk.parse_version(LOCKED))
