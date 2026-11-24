"""launch.py: builds the ``plurai ci run`` command line and replaces itself with it (exec)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import SCRIPTS, STUB_DIR

BASE_ENV = {
    'SA_REPO': 'acme/agents',
    'SA_SELECTION': 'smoke',
    'SA_BASE': 'a' * 40,
    'SA_MAIN_RUN': '',
    'SA_VISIBILITY': 'private',
    'SA_INCLUDE_CONTENT_SETTING': 'auto',
    'SA_URL': 'https://plurai.example',
    'SA_TOKEN': 'tok-secret',
    'SA_BIN_DIR': '',
    'SA_ACTION_REF': 'v1',
    'PATH': f'{STUB_DIR}:{os.environ["PATH"]}',
}


@pytest.fixture
def launch(run_script, out_dir):
    def run(**overrides):
        env = {**BASE_ENV, **overrides}
        result = run_script('launch.py', env)
        record = out_dir / 'stub-record.json'
        return result, (json.loads(record.read_text()) if record.exists() else None)

    return run


def test_argv_carries_the_contract_flags(launch):
    result, record = launch()

    assert result.code == 0
    assert record['argv'] == [
        'ci',
        'run',
        '--repo',
        'acme/agents',
        '--selection',
        'smoke',
        '--base',
        'a' * 40,
        '--visibility',
        'private',
    ]


def test_main_run_flag_is_present_only_when_a_file_was_found(launch, tmp_path):
    main_run = tmp_path / 'main-run.json'
    main_run.write_text('{"main": true}')

    _, with_main = launch(SA_MAIN_RUN=str(main_run))
    _, without = launch()

    assert with_main['argv'][with_main['argv'].index('--main-run') :][:2] == ['--main-run', str(main_run)]
    assert with_main['main_run_content'] == '{"main": true}'
    assert '--main-run' not in without['argv']


def test_base_and_visibility_are_omitted_when_unknown(launch):
    _, record = launch(SA_BASE='', SA_VISIBILITY='')

    assert record['argv'] == ['ci', 'run', '--repo', 'acme/agents', '--selection', 'smoke']


@pytest.mark.parametrize('setting', ['true', 'false'])
def test_an_explicit_content_setting_is_passed_through(launch, setting):
    _, record = launch(SA_INCLUDE_CONTENT_SETTING=setting)

    assert record['argv'][-2:] == ['--include-content', setting]


ACTIONS_RUN = {
    'GITHUB_SERVER_URL': 'https://github.example',
    'GITHUB_REPOSITORY': 'acme/agents',
    'GITHUB_RUN_ID': '4242',
    'GITHUB_RUN_ATTEMPT': '3',
}


def test_the_run_url_is_the_runs_page_for_this_attempt(launch):
    _, record = launch(**ACTIONS_RUN)

    assert record['argv'][-2:] == ['--run-url', 'https://github.example/acme/agents/actions/runs/4242/attempts/3']


@pytest.mark.parametrize('missing', sorted(ACTIONS_RUN))
@pytest.mark.parametrize('value', ['', '  '])
def test_no_run_url_is_passed_when_any_part_of_it_is_missing(launch, missing, value):
    _, record = launch(**{**ACTIONS_RUN, missing: value})

    assert '--run-url' not in record['argv']


def test_no_run_url_is_passed_outside_github_actions(launch):
    _, record = launch()

    assert '--run-url' not in record['argv']


def test_a_trailing_slash_on_the_server_url_does_not_double_up(launch):
    _, record = launch(**{**ACTIONS_RUN, 'GITHUB_SERVER_URL': 'https://github.example/'})

    assert record['argv'][-1] == 'https://github.example/acme/agents/actions/runs/4242/attempts/3'


def test_the_run_url_comes_after_the_content_setting(launch):
    _, record = launch(SA_INCLUDE_CONTENT_SETTING='true', **ACTIONS_RUN)

    assert record['argv'][-4:-2] == ['--include-content', 'true'] and record['argv'][-2] == '--run-url'


def test_url_and_token_reach_the_command_under_the_sdk_names(launch):
    _, record = launch()

    assert record['env']['PLURAI_URL'] == 'https://plurai.example'
    assert record['env']['PLURAI_TOKEN'] == '<set>'
    assert 'SA_TOKEN' not in record['env']
    assert 'SA_URL' not in record['env']


@pytest.mark.parametrize('url', ['', '  '])
def test_no_url_means_no_plurai_url_reaches_the_command(launch, url):
    _, record = launch(SA_URL=url, PLURAI_URL='https://inherited.example')

    assert 'PLURAI_URL' not in record['env']
    assert 'SA_URL' not in record['env']


def test_no_token_means_no_plurai_token_variable(launch):
    _, record = launch(SA_TOKEN='', PLURAI_TOKEN='inherited')

    assert 'PLURAI_TOKEN' not in record['env']


def test_the_command_never_sees_github_credentials(launch):
    hostile = {
        'GITHUB_TOKEN': 'x',
        'GH_TOKEN': 'x',
        'ACTIONS_RUNTIME_TOKEN': 'x',
        'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'x',
        'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://example.invalid',
        'ACTIONS_RESULTS_URL': 'https://example.invalid',
        'GITHUB_ENV': '/tmp/env',  # noqa: S108
    }

    _, record = launch(**hostile)

    for name in [*hostile, 'GITHUB_OUTPUT', 'GITHUB_STEP_SUMMARY']:
        assert name not in record['env']


def test_the_agents_own_env_block_still_passes_through(launch):
    _, record = launch(OPENAI_API_KEY='sk-test')

    assert record['env']['OPENAI_API_KEY'] == '<set>'


def test_the_exit_code_of_the_command_is_the_exit_code_of_the_step(launch):
    for code in (1, 10, 20, 65, 77):
        result, _ = launch(STUB_PLURAI_EXIT=str(code))
        assert result.code == code


def test_the_working_directory_is_kept(launch, tmp_path):
    work = tmp_path / 'plurai_sim'
    work.mkdir()

    result = subprocess.run(
        [sys.executable, str(SCRIPTS / 'launch.py')],
        env={**BASE_ENV, 'PLURAI_CI_OUT_DIR': str(tmp_path / 'o')},
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert json.loads((tmp_path / 'o' / 'stub-record.json').read_text())['cwd'] == str(work.resolve())


def test_missing_executable_is_reported(run_script):
    result = run_script('launch.py', {**BASE_ENV, 'PATH': '/usr/bin:/bin'})

    assert result.code == 127
    assert 'plurai' in result.stderr


def test_bin_dir_is_searched_first(run_script, out_dir, tmp_path):
    bin_dir = tmp_path / 'venv-bin'
    bin_dir.mkdir()
    marker = bin_dir / 'plurai'
    marker.write_text(f'#!/bin/sh\necho picked > {tmp_path}/picked\n')
    marker.chmod(0o755)

    result = run_script('launch.py', {**BASE_ENV, 'SA_BIN_DIR': str(bin_dir)})

    assert result.code == 0
    assert (tmp_path / 'picked').read_text().strip() == 'picked'


def test_a_signal_reaches_the_command_through_exec(out_dir, tmp_path):
    env = {**BASE_ENV, 'PLURAI_CI_OUT_DIR': str(out_dir), 'STUB_PLURAI_WAIT': '30'}
    process = subprocess.Popen([sys.executable, str(SCRIPTS / 'launch.py')], env=env, cwd=tmp_path)
    waiting = Path(out_dir / 'stub-waiting')
    deadline = time.monotonic() + 20
    while not waiting.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert waiting.exists()

    assert int(waiting.read_text()) == process.pid
    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=20) == 130
    assert (out_dir / 'stub-signal').read_text() == 'SIGTERM'


def test_an_agent_env_name_with_an_action_prefix_reaches_the_agent(launch):
    _, record = launch(SA_REGION='eu-west-1', INPUT_LANGUAGE='en')

    assert record['env']['SA_REGION'] == 'eu-west-1'
    assert record['env']['INPUT_LANGUAGE'] == 'en'


def test_the_names_the_action_itself_sets_do_not_reach_the_agent(launch):
    own = {'SA_PYTHON': '/usr/bin/python3', 'INPUT_URL': 'https://plurai.example', 'SA_UV': '/usr/bin/uv'}

    _, record = launch(**own)

    for name in [*own, *BASE_ENV]:
        if name not in ('PATH',):
            assert name not in record['env'], name


def test_the_action_ref_reaches_the_command_as_the_action_version(launch):
    _, record = launch(SA_ACTION_REF='v1.2.3')

    assert record['env']['PLURAI_ACTION_VERSION'] == 'v1.2.3'
    assert 'SA_ACTION_REF' not in record['env']


def test_a_sha_ref_is_passed_as_it_is(launch):
    _, record = launch(SA_ACTION_REF='f' * 40)

    assert record['env']['PLURAI_ACTION_VERSION'] == 'f' * 40


def test_an_empty_ref_leaves_the_action_version_unset(launch):
    _, record = launch(SA_ACTION_REF=' ')

    assert 'PLURAI_ACTION_VERSION' not in record['env']


def test_the_action_version_is_the_ref_and_nothing_inherited(launch):
    _, from_ref = launch(SA_ACTION_REF='v1', PLURAI_ACTION_VERSION='spoofed')
    _, no_ref = launch(SA_ACTION_REF='', PLURAI_ACTION_VERSION='spoofed')

    assert from_ref['env']['PLURAI_ACTION_VERSION'] == 'v1'
    assert 'PLURAI_ACTION_VERSION' not in no_ref['env']


def test_the_action_version_carries_no_credential(launch):
    _, record = launch(SA_ACTION_REF='v1', GITHUB_TOKEN='ghs_x', SA_TOKEN='tok-secret')

    assert record['env']['PLURAI_ACTION_VERSION'] == 'v1'
    assert 'tok-secret' not in record['env']['PLURAI_ACTION_VERSION']
