"""Shared helpers: run a script as a subprocess in a clean environment, and build GitHub event payloads."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / 'scripts'
STUB_DIR = REPO / 'tests' / 'stub'

SHA_A = 'a' * 40
SHA_B = 'b' * 40
SHA_C = 'c' * 40


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str
    outputs: dict[str, str]


def read_outputs(path: Path) -> dict[str, str]:
    """Parse a GITHUB_OUTPUT file (``name=value`` lines and ``name<<DELIM`` blocks)."""
    outputs: dict[str, str] = {}
    if not path.exists():
        return outputs
    lines = path.read_text().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if '<<' in line.split('=', 1)[0]:
            name, delim = line.split('<<', 1)
            body: list[str] = []
            i += 1
            while lines[i] != delim:
                body.append(lines[i])
                i += 1
            outputs[name] = '\n'.join(body)
        elif '=' in line:
            name, value = line.split('=', 1)
            outputs[name] = value
        i += 1
    return outputs


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    path = tmp_path / 'plurai-ci'
    path.mkdir()
    return path


@pytest.fixture
def run_script(tmp_path: Path, out_dir: Path):
    """Run ``scripts/<name>`` with exactly the given environment (plus PATH and a scratch GITHUB_OUTPUT)."""

    def run(name: str, env: Mapping[str, str] | None = None, *args: str, cwd: Path | None = None) -> Result:
        github_output = tmp_path / 'github_output'
        github_summary = tmp_path / 'github_step_summary'
        full_env = {
            'PATH': os.environ['PATH'],
            'GITHUB_OUTPUT': str(github_output),
            'GITHUB_STEP_SUMMARY': str(github_summary),
            'PLURAI_CI_OUT_DIR': str(out_dir),
            'RUNNER_TEMP': str(tmp_path),
            **(env or {}),
        }
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / name), *args],
            env=full_env,
            capture_output=True,
            text=True,
            cwd=cwd or tmp_path,
            timeout=60,
            check=False,
        )
        return Result(proc.returncode, proc.stdout, proc.stderr, read_outputs(github_output))

    return run


def write_event(tmp_path: Path, payload: dict) -> str:
    path = tmp_path / 'event.json'
    path.write_text(json.dumps(payload))
    return str(path)


@pytest.fixture
def github():
    from fake_github import FakeGitHub

    fake = FakeGitHub()
    fake.start()
    yield fake
    fake.stop()
