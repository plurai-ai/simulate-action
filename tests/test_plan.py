"""plan.py: validates the inputs, maps the event to a selection and decides what the later steps do."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from conftest import SHA_A, SHA_B, write_event


@pytest.fixture
def plan(run_script, tmp_path):
    def run(event_name='push', payload=None, extra_env=None, **inputs):
        env = {
            'GITHUB_EVENT_NAME': event_name,
            'GITHUB_EVENT_PATH': write_event(tmp_path, payload or {}),
            'GITHUB_REPOSITORY': 'acme/agents',
            'GITHUB_ACTOR': 'octocat',
            'INPUT_URL': 'https://plurai.example',
            'INPUT_SELECTION': '',
            'INPUT_INCLUDE_CONTENT': 'auto',
            'INPUT_RETENTION_DAYS': '7',
        }
        env.update({f'INPUT_{key.upper()}': value for key, value in inputs.items()})
        env.update(extra_env or {})
        return run_script('plan.py', env)

    return run


@pytest.mark.parametrize('url', ['', '  '])
def test_an_empty_url_is_not_refused(plan, url):
    result = plan(url=url)

    assert result.code == 0
    assert result.outputs['selection'] == 'full'


def test_a_refusal_writes_the_summary_and_the_comment(plan, out_dir):
    result = plan(retention_days='0')

    assert result.code == 65
    assert '`retention_days`' in result.stderr
    heading = (out_dir / 'summary.md').read_text().splitlines()[0]
    assert heading.startswith('## Plurai simulation')
    assert '⛔ refused' in heading
    assert '`retention_days`' in heading
    assert (out_dir / 'comment.md').read_text().startswith('<!-- plurai-simulation-report -->')
    assert result.outputs['out_dir'] == str(out_dir)


@pytest.mark.parametrize(
    ('event', 'selection'),
    [('pull_request', 'smoke'), ('merge_group', 'smoke'), ('push', 'full'), ('schedule', 'full'), ('workflow_dispatch', 'smoke')],
)
def test_event_maps_to_selection(plan, event, selection):
    result = plan(event_name=event)

    assert result.code == 0
    assert result.outputs['selection'] == selection


def test_an_empty_dispatch_selection_is_smoke(plan):
    result = plan(event_name='workflow_dispatch', selection='')

    assert result.code == 0
    assert result.outputs['selection'] == 'smoke'


def test_a_blank_dispatch_selection_is_smoke(plan):
    assert plan(event_name='workflow_dispatch', selection='  ').outputs['selection'] == 'smoke'


def test_dispatch_input_overrides_the_default(plan):
    assert plan(event_name='workflow_dispatch', selection='full').outputs['selection'] == 'full'


def test_dispatch_input_must_be_smoke_or_full(plan):
    result = plan(event_name='workflow_dispatch', selection='everything')

    assert result.code == 65
    assert '`selection`' in result.stderr


def test_selection_input_is_ignored_off_dispatch_with_a_warning(plan):
    result = plan(event_name='pull_request', selection='full')

    assert result.outputs['selection'] == 'smoke'
    assert '::warning::' in result.stdout


def test_unsupported_event_is_refused(plan):
    result = plan(event_name='pull_request_target')

    assert result.code == 65
    assert 'pull_request_target' in result.stderr


@pytest.mark.parametrize(
    ('payload', 'visibility'),
    [
        ({'repository': {'visibility': 'private'}}, 'private'),
        ({'repository': {'visibility': 'internal'}}, 'internal'),
        ({'repository': {'visibility': 'public'}}, 'public'),
        ({'repository': {}}, ''),
    ],
)
def test_visibility_comes_from_the_event_repository(plan, payload, visibility):
    assert plan(payload=payload).outputs['visibility'] == visibility


@pytest.mark.parametrize(
    ('setting', 'visibility', 'included'),
    [
        ('auto', 'private', 'true'),
        ('auto', 'public', 'false'),
        ('auto', 'internal', 'false'),
        ('auto', '', 'false'),
        ('true', 'public', 'true'),
        ('false', 'private', 'false'),
    ],
)
def test_include_content_resolves_from_the_visibility(plan, setting, visibility, included):
    result = plan(payload={'repository': {'visibility': visibility} if visibility else {}}, include_content=setting)

    assert result.outputs['include_content'] == included
    assert result.outputs['include_content_setting'] == setting


def test_invalid_include_content_is_refused(plan):
    result = plan(include_content='maybe')

    assert result.code == 65
    assert '`include_content`' in result.stderr


@pytest.mark.parametrize('days', ['0', '91', 'seven', '-1'])
def test_retention_days_must_be_1_to_90(plan, days):
    result = plan(retention_days=days)

    assert result.code == 65
    assert '`retention_days`' in result.stderr


def test_merge_group_base_is_the_event_base_sha(plan):
    result = plan(event_name='merge_group', payload={'merge_group': {'base_sha': SHA_A, 'head_sha': SHA_B}})

    assert result.outputs['base_strategy'] == 'event'
    assert result.outputs['base'] == SHA_A


def test_push_base_is_the_before_sha(plan):
    result = plan(event_name='push', payload={'before': SHA_A, 'after': SHA_B})

    assert (result.outputs['base_strategy'], result.outputs['base']) == ('event', SHA_A)


def test_push_to_a_new_branch_has_no_base(plan):
    result = plan(event_name='push', payload={'before': '0' * 40})

    assert (result.outputs['base_strategy'], result.outputs['base']) == ('none', '')


def test_pull_request_base_is_the_merge_base_of_the_two_shas(plan):
    payload = {
        'pull_request': {'number': 7, 'base': {'sha': SHA_A}, 'head': {'sha': SHA_B, 'repo': {'full_name': 'acme/agents'}}}
    }

    result = plan(event_name='pull_request', payload=payload)

    assert result.outputs['base_strategy'] == 'merge-base'
    assert (result.outputs['pr_base_sha'], result.outputs['pr_head_sha']) == (SHA_A, SHA_B)


@pytest.mark.parametrize('event', ['schedule', 'workflow_dispatch'])
def test_schedule_and_dispatch_base_is_the_last_main_run(plan, event):
    assert plan(event_name=event).outputs['base_strategy'] == 'last-main-run'


def test_outputs_name_the_out_dir_and_the_workflow_artifact_key(plan, out_dir):
    result = plan()

    assert result.outputs['out_dir'] == str(out_dir)


def test_workflow_key_carries_the_job_id(run_script, tmp_path):
    env = {
        'GITHUB_EVENT_NAME': 'push',
        'GITHUB_EVENT_PATH': write_event(tmp_path, {}),
        'GITHUB_WORKFLOW_REF': 'acme/agents/.github/workflows/plurai-support.yml@refs/heads/main',
        'GITHUB_JOB': 'simulate',
        'INPUT_URL': 'https://plurai.example',
    }

    assert run_script('plan.py', env).outputs['workflow_key'] == 'plurai-support-simulate'


def make_tool(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text('#!/bin/sh\n')
    path.chmod(0o755)
    return path


def test_plan_resolves_the_trusted_tools_to_absolute_paths_for_the_later_steps(plan, tmp_path):
    bin_dir = tmp_path / 'tools'
    uv, git = make_tool(bin_dir, 'uv'), make_tool(bin_dir, 'git')

    result = plan(extra_env={'PATH': f'{bin_dir}:/usr/bin:/bin'})

    assert result.outputs['uv'] == str(uv)
    assert result.outputs['git'] == str(git)
    assert result.outputs['python'].startswith('/')
    assert Path(result.outputs['python']).exists()


def test_plan_leaves_a_missing_tool_empty(plan, tmp_path):
    result = plan(extra_env={'PATH': '/nonexistent'})

    assert result.outputs['uv'] == ''
    assert result.outputs['git'] == ''


def test_a_refused_plan_still_names_the_workflow_key_and_the_artifact_inputs(run_script, tmp_path):
    env = {
        'GITHUB_EVENT_NAME': 'push',
        'GITHUB_EVENT_PATH': write_event(tmp_path, {}),
        'GITHUB_WORKFLOW_REF': 'acme/agents/.github/workflows/plurai-support.yml@refs/heads/main',
        'GITHUB_JOB': 'simulate',
        'INPUT_RETENTION_DAYS': '0',
    }

    result = run_script('plan.py', env)

    assert result.code == 65
    assert result.outputs['workflow_key'] == 'plurai-support-simulate'
    assert result.outputs['include_content'] == 'false'


PULL_REQUEST = {
    'pull_request': {
        'number': 7,
        'base': {'sha': SHA_A, 'ref': 'release/2.x'},
        'head': {'sha': SHA_B, 'repo': {'full_name': 'acme/agents'}},
    },
    'repository': {'default_branch': 'main', 'visibility': 'private'},
}


def test_a_pull_request_into_another_branch_compares_with_that_branch(plan):
    result = plan(event_name='pull_request', payload=PULL_REQUEST)

    assert result.outputs['main_branch'] == 'release/2.x'
    assert result.outputs['default_branch'] == 'main'


def test_a_pull_request_into_the_default_branch_compares_with_it(plan):
    payload = {**PULL_REQUEST, 'pull_request': {**PULL_REQUEST['pull_request'], 'base': {'sha': SHA_A, 'ref': 'main'}}}

    assert plan(event_name='pull_request', payload=payload).outputs['main_branch'] == 'main'


def test_a_merge_group_compares_with_the_branch_it_merges_into(plan):
    payload = {'merge_group': {'base_sha': SHA_A, 'base_ref': 'refs/heads/release/2.x'}, 'repository': {'default_branch': 'main'}}

    assert plan(event_name='merge_group', payload=payload).outputs['main_branch'] == 'release/2.x'


def test_a_push_compares_with_the_default_branch(plan):
    payload = {'before': SHA_A, 'repository': {'default_branch': 'trunk'}}

    assert plan(event_name='push', payload=payload).outputs['main_branch'] == 'trunk'


def test_a_schedule_event_has_no_repository_so_the_default_branch_is_the_ref_it_runs_on(plan):
    result = plan(event_name='schedule', payload={'schedule': '0 3 * * *'}, extra_env={'GITHUB_REF_NAME': 'trunk'})

    assert result.outputs['default_branch'] == 'trunk'
    assert result.outputs['main_branch'] == 'trunk'


def test_the_event_s_default_branch_wins_over_the_ref_name(plan):
    payload = {'repository': {'default_branch': 'trunk'}}

    result = plan(event_name='workflow_dispatch', payload=payload, extra_env={'GITHUB_REF_NAME': 'feature/x'})

    assert result.outputs['default_branch'] == 'trunk'


def test_a_matrix_job_is_refused_because_its_legs_would_overwrite_each_other(plan, out_dir):
    result = plan(extra_env={'SA_JOB_TOTAL': '3', 'SA_JOB_INDEX': '1'})

    assert result.code == 65
    assert 'matrix' in result.stderr
    assert 'matrix' in (out_dir / 'summary.md').read_text()


@pytest.mark.parametrize('total', ['', '1'])
def test_a_job_without_a_matrix_is_planned(plan, total):
    assert plan(extra_env={'SA_JOB_TOTAL': total}).code == 0


def test_an_interpreter_under_the_workspace_is_refused(tmp_path, monkeypatch):
    import plan as plan_module
    from common import Refusal

    workspace = tmp_path / 'workspace'
    (workspace / '.venv' / 'bin').mkdir(parents=True)
    python = workspace / '.venv' / 'bin' / 'python3'
    python.symlink_to(Path(sys.executable))  # a venv's interpreter is a link to a system python
    monkeypatch.setattr(plan_module.sys, 'executable', str(python))

    with pytest.raises(Refusal, match='workspace'):
        plan_module.plan({'GITHUB_WORKSPACE': str(workspace)})


def test_an_interpreter_outside_the_workspace_is_trusted(plan, tmp_path):
    result = plan(extra_env={'GITHUB_WORKSPACE': str(tmp_path / 'checkout')})

    assert result.code == 0
