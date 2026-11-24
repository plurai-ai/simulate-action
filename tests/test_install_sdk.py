"""install_sdk.py: finds the project's interpreter, uses an SDK in the pin's range, refuses others, else installs the pin."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import install_sdk
import pytest

PIN = '1.0.0rc1'


def make_executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


FAKE_PYTHON = """#!/bin/sh
env >> "${FAKE_ENV_LOG:-/dev/null}"
case "$*" in
  *platform*) echo "${FAKE_PY_VERSION:-3.12.4}" ;;
  *metadata*) if [ -n "$FAKE_SDK_VERSION" ]; then echo "$FAKE_SDK_VERSION"; else exit 1; fi ;;
  *"-m pip"*) printf '%s\\n' "$*" >> "$FAKE_LOG"; env | grep -E '^PIP_' >> "$FAKE_LOG" || true ;;
esac
"""

FAKE_UV = """#!/bin/sh
printf 'uv %s\\n' "$*" >> "$FAKE_LOG"
env | grep -E '^UV_INDEX' >> "$FAKE_LOG" || true
env >> "${FAKE_ENV_LOG:-/dev/null}"
exit "${FAKE_UV_EXIT:-0}"
"""


@pytest.fixture
def project(tmp_path):
    workspace = tmp_path / 'workspace'
    workdir = workspace / 'agents' / 'support' / 'plurai_sim'
    workdir.mkdir(parents=True)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    return workspace, workdir, bin_dir


@pytest.fixture
def install(run_script, project, tmp_path):
    workspace, workdir, bin_dir = project
    release = tmp_path / 'release.json'
    log = tmp_path / 'fake.log'

    def run(source='pypi', uv=True, venv='agents/support/.venv', pin=PIN, **env):
        release.write_text(json.dumps({'sdk': {'version': pin, 'source': source}, 'managed_rev': 1}))
        if venv:
            make_executable(workspace / venv / 'bin' / 'python', FAKE_PYTHON)
        if uv:
            make_executable(bin_dir / 'uv', FAKE_UV)
        full = {
            'PATH': f'{bin_dir}:/usr/bin:/bin',
            'GITHUB_WORKSPACE': str(workspace),
            'SA_WORKDIR': str(workdir),
            'SA_RELEASE_FILE': str(release),
            'SA_UV': str(bin_dir / 'uv') if uv else '',
            'FAKE_LOG': str(log),
            **env,
        }
        result = run_script('install_sdk.py', full)
        result.log = log.read_text() if log.exists() else ''
        return result

    return run


def test_the_interpreter_is_the_nearest_project_venv(install, project):
    workspace, _, _ = project
    make_executable(workspace / 'agents' / 'support' / 'plurai_sim' / '.venv' / 'bin' / 'python', FAKE_PYTHON)

    result = install()

    assert result.outputs['python'] == str(workspace / 'agents' / 'support' / 'plurai_sim' / '.venv' / 'bin' / 'python')
    assert result.outputs['bin_dir'] == str(workspace / 'agents' / 'support' / 'plurai_sim' / '.venv' / 'bin')


def test_a_venv_further_up_is_found(install, project):
    workspace, _, _ = project

    result = install(venv='.venv')

    assert result.outputs['python'] == str(workspace / '.venv' / 'bin' / 'python')


def test_virtual_env_is_the_next_choice(install, project, tmp_path):
    active = make_executable(tmp_path / 'active' / 'bin' / 'python', FAKE_PYTHON)

    result = install(venv=None, VIRTUAL_ENV=str(tmp_path / 'active'))

    assert result.outputs['python'] == str(active)


def test_python_older_than_3_12_is_refused(install):
    result = install(FAKE_PY_VERSION='3.11.9')

    assert result.code == 65
    assert '3.12' in result.stderr


def test_an_sdk_below_the_range_is_refused_and_the_range_is_named(install):
    result = install(FAKE_SDK_VERSION='0.9.0')

    assert result.code == 65
    assert '0.9.0' in result.stderr
    assert f'>={PIN},<1.1.0' in result.stderr
    assert result.log == ''


@pytest.mark.parametrize('present', ['1.1.0', '1.1.0.dev0', '2.0.0', '1.0.0.dev9', '1.0.0a1', '1.0.0b3'])
def test_an_sdk_of_another_minor_or_below_the_pin_is_refused(install, present):
    result = install(FAKE_SDK_VERSION=present)

    assert result.code == 65
    assert present in result.stderr
    assert result.log == ''


@pytest.mark.parametrize('present', [PIN, '1.0.0rc2', '1.0.0', '1.0.0.post1', '1.0.7'])
def test_an_sdk_in_the_range_is_used_as_is(install, present):
    result = install(FAKE_SDK_VERSION=present)

    assert result.code == 0
    assert result.log == ''
    assert result.outputs['installed'] == 'already'
    assert result.outputs['sdk_version'] == present


def test_without_an_sdk_the_pin_is_installed_and_reported(install):
    result = install()

    assert result.code == 0
    assert result.outputs['installed'] == 'installed'
    assert result.outputs['sdk_version'] == PIN


def test_an_unreadable_installed_version_is_refused(install):
    result = install(FAKE_SDK_VERSION='not-a-version')

    assert result.code == 65
    assert 'not-a-version' in result.stderr


def test_the_range_follows_the_pin_of_a_dev_build(install):
    assert install(pin='1.1.0.dev405', FAKE_SDK_VERSION='1.1.0.dev405').outputs['installed'] == 'already'
    assert install(pin='1.1.0.dev405', FAKE_SDK_VERSION='1.1.0rc1').outputs['installed'] == 'already'
    assert install(pin='1.1.0.dev405', FAKE_SDK_VERSION='1.1.3').outputs['installed'] == 'already'
    assert install(pin='1.1.0.dev405', FAKE_SDK_VERSION='1.1.0.dev404').code == 65
    assert install(pin='1.1.0.dev405', FAKE_SDK_VERSION='1.2.0.dev0').code == 65


@pytest.mark.parametrize(
    'lock_name, text',
    [
        ('uv.lock', '[[package]]\nname = "plurai-simulation-sdk"\nversion = "0.9.0"\n'),
        ('poetry.lock', '[[package]]\nname = "plurai_simulation_sdk"\nversion = "0.2.0"\n'),
        ('requirements.txt', 'plurai-simulation-sdk==0.3.0\n'),
    ],
)
def test_lockfiles_are_not_read(install, project, lock_name, text):
    workspace, workdir, _ = project
    (workspace / lock_name).write_text(text)
    (workdir / lock_name).write_text(text)

    result = install()

    assert result.code == 0
    assert result.outputs['installed'] == 'installed'


def test_the_pin_is_installed_with_uv_by_exact_version_and_no_index_override(install):
    result = install()

    assert result.code == 0
    first = result.log.splitlines()[0]
    assert first.startswith('uv pip install --python ')
    assert f'plurai-simulation-sdk=={PIN}' in first
    assert 'UV_INDEX' not in result.log


@pytest.mark.parametrize('source', ['testpypi', ''])
def test_a_source_other_than_pypi_is_refused_before_anything_is_installed(install, source):
    result = install(source=source)

    assert result.code == 65
    assert 'release.json' in result.stderr
    assert 'PyPI' in result.stderr
    assert result.log == ''


def test_an_sdk_in_range_is_used_as_is_without_uv(install):
    """Nothing is installed, so no installer is needed."""
    result = install(uv=False, FAKE_SDK_VERSION='1.0.3')

    assert result.code == 0, result.stderr
    assert result.outputs['installed'] == 'already'
    assert result.outputs['sdk_version'] == '1.0.3'
    assert result.log == ''


def test_without_uv_the_pypi_release_still_installs_with_pip(install):
    result = install(uv=False)

    assert result.code == 0
    assert f'-m pip install plurai-simulation-sdk=={PIN}' in result.log
    assert 'PIP_INDEX_URL' not in result.log
    assert 'PIP_EXTRA_INDEX_URL' not in result.log


def test_an_install_that_fails_writes_the_documents_that_say_what_stopped(install, out_dir):
    result = install(FAKE_UV_EXIT='3')

    assert result.code == 65
    assert 'installing plurai-simulation-sdk' in result.stderr
    summary = (out_dir / 'summary.md').read_text()
    assert summary.startswith('## Plurai simulation · ⛔ refused: installing plurai-simulation-sdk')
    assert 'exit 3' in summary
    assert (out_dir / 'comment.md').read_text().startswith('<!-- plurai-simulation-report -->')


def test_skip_install_runs_the_checks_but_not_the_install(install):
    result = install(PLURAI_ACTION_SKIP_INSTALL='1', FAKE_SDK_VERSION='0.9.0')
    assert result.code == 65

    result = install(PLURAI_ACTION_SKIP_INSTALL='1')
    assert result.code == 0
    assert result.log == ''
    assert result.outputs['installed'] == 'skipped'


def test_no_interpreter_at_all_is_refused(install, project):
    result = install(venv=None, PATH='/nonexistent')

    assert result.code == 65


def test_python3_on_path_is_the_last_resort(install, project):
    _, _, bin_dir = project
    make_executable(bin_dir / 'python3', FAKE_PYTHON)

    result = install(venv=None)

    assert result.outputs['python'] == str(bin_dir / 'python3')


def test_the_probes_of_the_project_interpreter_never_see_an_oidc_request(install, tmp_path):
    probe_log = tmp_path / 'probe-env.log'

    result = install(
        FAKE_ENV_LOG=str(probe_log),
        ACTIONS_ID_TOKEN_REQUEST_URL='https://token.example/oidc',
        ACTIONS_ID_TOKEN_REQUEST_TOKEN='oidc-request-token',
        FAKE_SDK_VERSION=PIN,
    )

    assert result.code == 0
    seen = probe_log.read_text()
    assert 'PATH=' in seen
    for secret in ('oidc-request-token', 'token.example'):
        assert secret not in seen


def test_the_install_subprocess_never_gets_an_oidc_request(install, tmp_path):
    install_env_log = tmp_path / 'install-env.log'

    result = install(
        FAKE_ENV_LOG=str(install_env_log),
        ACTIONS_ID_TOKEN_REQUEST_URL='https://token.example/oidc',
        ACTIONS_ID_TOKEN_REQUEST_TOKEN='oidc-request-token',
    )

    assert result.code == 0
    lines = install_env_log.read_text().splitlines()
    assert 'PATH=' in ''.join(lines)
    assert not any(line.startswith('ACTIONS_ID_TOKEN_REQUEST_') for line in lines)


STEP_FILES_AND_RUNTIME_TOKENS = {
    'GITHUB_TOKEN': 'gh-token',
    'GH_TOKEN': 'gh-token',
    'ACTIONS_RUNTIME_TOKEN': 'runtime-token',
    'ACTIONS_RESULTS_URL': 'https://results.example',
    'GITHUB_ENV': '/runner/env',
    'GITHUB_PATH': '/runner/path',
    'GITHUB_STATE': '/runner/state',
}


def test_the_probes_see_none_of_the_runner_credentials_or_step_files(install, tmp_path):
    probe_log = tmp_path / 'probe-env.log'

    install(FAKE_ENV_LOG=str(probe_log), FAKE_SDK_VERSION=PIN, **STEP_FILES_AND_RUNTIME_TOKENS)

    seen = probe_log.read_text()
    for name, value in STEP_FILES_AND_RUNTIME_TOKENS.items():
        assert f'{name}=' not in seen
        assert value not in seen
    assert 'GITHUB_OUTPUT=' not in seen
    assert 'GITHUB_STEP_SUMMARY=' not in seen


def test_uv_is_the_one_the_plan_step_resolved_never_a_path_lookup(install, project):
    _, _, bin_dir = project
    make_executable(bin_dir / 'uv', FAKE_UV)

    result = install(uv=False)

    assert result.code == 0
    assert f'-m pip install plurai-simulation-sdk=={PIN}' in result.log
    assert 'uv pip install' not in result.log


def test_versions_order_as_pep_440_does_for_dev_pre_and_post_releases():
    ordered = [
        '1.1.0.dev0',
        '1.1.0.dev405',
        '1.1.0a1',
        '1.1.0b2',
        '1.1.0rc1',
        '1.1.0rc2',
        '1.1.0',
        '1.1.0.post1',
        '1.1.1.dev1',
        '1.1.1',
        '1.2.0.dev0',
        '2.0.0',
    ]
    keys = [install_sdk.parse_version(text).key for text in ordered]

    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys)


@pytest.mark.parametrize(
    'left, right',
    [
        ('1.1.0-dev.0', '1.1.0.dev0'),
        ('1.1.0-rc.1', '1.1.0rc1'),
        ('1.1.0', '1.1'),
        ('1.1.0-dev.405', '1.1.0.dev405'),
    ],
)
def test_the_spellings_of_one_version_compare_equal(left, right):
    assert install_sdk.parse_version(left).key == install_sdk.parse_version(right).key


@pytest.mark.parametrize('text', ['', 'abc', '1.x.0', '1.1.0-foo'])
def test_a_text_that_is_not_a_version_is_rejected(text):
    with pytest.raises(ValueError, match='not a version'):
        install_sdk.parse_version(text)
