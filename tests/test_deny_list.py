"""One deny-list of credential and step-file variables, shared by every script that starts a process of project code."""

from __future__ import annotations

import sys

import yaml
from conftest import REPO, SCRIPTS

sys.path.insert(0, str(SCRIPTS))

import common
import install_sdk
import launch


def test_launch_and_install_use_the_same_deny_list():
    assert launch.STRIPPED is common.STRIPPED
    assert install_sdk.scrubbed({name: 'x' for name in common.STRIPPED}) == {}


def test_the_deny_list_names_the_runner_credentials_and_step_files():
    for name in (
        'GITHUB_TOKEN',
        'ACTIONS_RUNTIME_TOKEN',
        'ACTIONS_ID_TOKEN_REQUEST_URL',
        'ACTIONS_ID_TOKEN_REQUEST_TOKEN',
        'GITHUB_ENV',
        'GITHUB_PATH',
        'GITHUB_OUTPUT',
        'GITHUB_STATE',
        'GITHUB_STEP_SUMMARY',
    ):
        assert name in common.STRIPPED


def test_scrubbed_drops_the_deny_list_and_keeps_the_rest():
    env = {'PATH': '/bin', 'HOME': '/h', 'GITHUB_TOKEN': 't', 'SA_PYTHON': '/usr/bin/python3'}

    assert install_sdk.scrubbed(env) == {'PATH': '/bin', 'HOME': '/h'}


def test_every_variable_the_action_sets_for_itself_is_on_the_deny_list():
    """The agent keeps its own `SA_*` and `INPUT_*` names; only what the action's steps set is withheld."""
    own: set[str] = set()
    for path in (REPO / 'action.yml', REPO / 'comment' / 'action.yml'):
        for step in yaml.safe_load(path.read_text())['runs']['steps']:
            own |= {name for name in step.get('env', {}) if name.startswith(('SA_', 'INPUT_'))}

    assert own
    assert own <= common.STRIPPED, sorted(own - common.STRIPPED)
