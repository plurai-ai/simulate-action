"""action.yml and the workflows: the structural rules that no script test can see."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from conftest import REPO

ACTION = yaml.safe_load((REPO / 'action.yml').read_text())
STEPS = {step.get('id') or step['name']: step for step in ACTION['runs']['steps']}
WORKFLOWS = sorted((REPO / '.github').rglob('*.yml'))
SHA = re.compile(r'^[^@]+@[0-9a-f]{40}$')
#: The repository's own action by name: the one job that runs it the way a customer does names it by branch.
OWN_ACTION = re.compile(r'^plurai-ai/simulate-action(/[^@]+)?@')


def uses(document: dict) -> list[str]:
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get('uses'), str):
                found.append(node['uses'])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(document)
    return found


COMMENT = yaml.safe_load((REPO / 'comment' / 'action.yml').read_text())
COMMENT_STEPS = {step.get('id') or step['name']: step for step in COMMENT['runs']['steps']}
ORDER = [step.get('id') or step['name'] for step in ACTION['runs']['steps']]


@pytest.mark.parametrize(
    'path', [REPO / 'action.yml', REPO / 'comment' / 'action.yml', *WORKFLOWS], ids=lambda p: str(p.relative_to(REPO))
)
def test_every_third_party_action_is_pinned_by_full_sha(path: Path):
    for reference in uses(yaml.safe_load(path.read_text())):
        if reference.startswith('./') or OWN_ACTION.match(reference):
            continue
        assert SHA.match(reference), f'{reference} is not pinned by a full sha'


def test_the_action_sets_no_timeout():
    assert not [name for name, step in STEPS.items() if 'timeout-minutes' in step]


def test_the_token_input_is_in_the_environment_of_the_run_step_only():
    holders = [name for name, step in STEPS.items() if 'inputs.token' in str(step)]

    assert holders == ['run']
    assert STEPS['run']['env']['SA_TOKEN'] == '${{ inputs.token }}'


def test_the_run_step_holds_no_github_token():
    text = str(STEPS['run'])

    assert 'github.token' not in text
    assert 'GITHUB_TOKEN' not in text
    assert 'secrets.' not in text


def test_the_run_step_replaces_its_shell_with_the_command():
    assert STEPS['run']['run'].startswith('exec ')


def test_the_job_token_is_only_in_the_fetch_step():
    holders = {name for name, step in STEPS.items() if 'github.token' in str(step) or 'GITHUB_TOKEN' in str(step)}

    assert holders == {'fetch'}


def after_the_run_step():
    return {name: step for name, step in STEPS.items() if ORDER.index(name) > ORDER.index('run')}


def test_no_step_after_the_run_step_holds_a_credential():
    steps = after_the_run_step()

    assert steps
    for name, step in steps.items():
        text = str(step)
        assert 'github.token' not in text, name
        assert 'GITHUB_TOKEN' not in text, name
        assert 'secrets.' not in text, name
        assert 'inputs.token' not in text, name
        assert 'access_token' not in text, name


def test_no_step_after_the_run_step_talks_to_the_github_api_or_the_pr():
    for name, step in after_the_run_step().items():
        assert 'comment.py' not in str(step), name
        assert 'pending' not in str(step), name


def test_the_action_has_no_detached_watcher():
    assert not (REPO / 'scripts' / 'pending.py').exists()
    assert not [name for name, step in STEPS.items() if 'pending' in str(step)]


def test_the_action_has_no_comment_input_or_output():
    assert 'pr_comment' not in ACTION['inputs']
    assert 'comment-url' not in ACTION['outputs']


def test_the_token_holding_fetch_runs_before_any_step_that_runs_project_code():
    assert ORDER.index('fetch') < ORDER.index('install') < ORDER.index('run')


def test_nothing_is_exported_to_the_job_environment():
    assert 'GITHUB_ENV' not in (REPO / 'action.yml').read_text()
    scripts = [
        path for path in (REPO / 'scripts').glob('*.py') if path.name != 'common.py'
    ]  # common.py only lists the names it strips
    assert not [path.name for path in scripts if 'GITHUB_ENV' in path.read_text()]


def test_the_report_artifact_carries_what_the_report_job_needs():
    paths = str(STEPS['Upload run.json and the reports']['with']['path'])

    for name in ('run.json', 'comment-final.md', 'suite-diff.md'):
        assert name in paths


def test_the_comment_action_reads_its_artifact_by_name_from_the_plan():
    download = COMMENT_STEPS['download']

    assert download['uses'].startswith('actions/download-artifact@')
    assert download['with']['name'] == '${{ steps.plan.outputs.artifact }}'
    assert download['if'] == "steps.plan.outputs.comment_mode == 'on' || steps.plan.outputs.report_mode == 'on'"


def test_the_comment_action_holds_the_job_token_in_the_post_and_check_steps_only():
    holders = {name for name, step in COMMENT_STEPS.items() if 'github.token' in str(step)}

    assert holders == {'post', 'check'}


def test_the_comment_action_uses_no_third_party_action_but_the_download():
    assert [step['uses'] for step in COMMENT['runs']['steps'] if 'uses' in step] == [
        'actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333'
    ]


def test_the_check_step_runs_our_script_on_the_downloaded_report_with_the_planned_values():
    step = COMMENT_STEPS['check']

    assert step['run'] == 'python3 -S -E "$GITHUB_ACTION_PATH/../scripts/check_run.py"'
    assert step['if'] == "steps.plan.outputs.report_mode == 'on'"
    assert step['env']['SA_CHECK_NAME'] == '${{ steps.plan.outputs.check_name }}'
    assert step['env']['SA_REPORT_DIR'] == COMMENT_STEPS['post']['env']['SA_REPORT_DIR']
    assert step['env']['SA_HEAD_SHA'] == '${{ steps.plan.outputs.head_sha }}'
    assert COMMENT['outputs']['check-url']['value'] == '${{ steps.check.outputs.check_url }}'


def test_the_check_step_runs_after_a_failed_download_so_a_missing_report_leaves_its_note():
    """A report artifact that is not there at all fails the download (which continues on error); the check script is what
    says "no check run: no report", so no condition of the check step may depend on the download's outcome."""
    assert COMMENT_STEPS['download']['continue-on-error'] is True
    assert 'download' not in COMMENT_STEPS['check']['if']


def test_the_check_step_holds_the_job_token_in_its_environment_only_and_no_secret():
    step = COMMENT_STEPS['check']

    assert step['env']['GITHUB_TOKEN'] == '${{ github.token }}'
    assert 'secrets.' not in str(step)
    assert 'inputs.token' not in str(step)
    assert 'continue-on-error' not in step  # the script never fails the job


def test_the_check_runs_after_the_comment_so_a_check_fault_never_delays_it():
    order = list(COMMENT_STEPS)

    assert order.index('post') < order.index('check')


def test_the_comment_action_runs_no_project_code_and_no_plurai_command():
    for name, step in COMMENT_STEPS.items():
        assert 'plurai ci' not in str(step), name
        assert 'working-directory' not in step, name
        assert 'inputs.token' not in str(step), name


def test_the_comment_action_names_the_simulate_job_by_default():
    assert COMMENT['inputs']['job']['default'] == 'simulate'
    assert COMMENT['inputs']['pr_comment']['default'] == 'true'


@pytest.mark.parametrize('name', ['Upload run.json and the reports', 'Upload transcripts'])
def test_uploads_run_even_after_a_failure(name):
    assert STEPS[name]['if'].startswith('always()')


def test_transcripts_are_uploaded_only_when_content_is_included():
    assert "include_content == 'true'" in STEPS['Upload transcripts']['if']
    assert STEPS['Upload transcripts']['with']['retention-days'] == '${{ steps.plan.outputs.retention_days }}'


def test_the_report_artifact_keeps_the_repository_retention():
    assert 'retention-days' not in STEPS['Upload run.json and the reports']['with']


def test_url_is_optional_and_empty_by_default():
    assert ACTION['inputs']['url']['required'] is False
    assert ACTION['inputs']['url']['default'] == ''


def test_the_fixture_workflow_uses_the_template_triggers_and_groups():
    workflow = yaml.safe_load((REPO / '.github' / 'workflows' / 'fixture.yml').read_text())
    triggers = workflow[True] if True in workflow else workflow['on']

    assert set(triggers) == {'pull_request', 'merge_group', 'push', 'schedule', 'workflow_dispatch'}
    group = workflow['concurrency']['group']
    for fragment in ('-pr-{0}', '-main-{0}', '-mq-{0}', 'github.sha', 'head_sha'):
        assert fragment in group
    assert workflow['concurrency']['cancel-in-progress'] == "${{ github.event_name == 'pull_request' }}"


FIXTURE = yaml.safe_load((REPO / '.github' / 'workflows' / 'fixture.yml').read_text())
REPORT_JOBS = {name: job for name, job in FIXTURE['jobs'].items() if name.endswith('-report')}


def test_the_fixture_grants_no_write_permission_to_a_job_that_runs_the_agent():
    assert FIXTURE['permissions'] == {'contents': 'read', 'actions': 'read'}
    for name, job in FIXTURE['jobs'].items():
        if name not in REPORT_JOBS:
            assert 'permissions' not in job, name


def test_the_fixture_has_a_report_job_per_comment_job_in_the_template_shape():
    assert set(REPORT_JOBS) == {'pr-comment-report', 'no-comment-report'}
    for name, job in REPORT_JOBS.items():
        assert job['needs'] == name.removesuffix('-report'), name
        assert job['if'].startswith('always()'), name
        assert job['permissions']['pull-requests'] == 'write', name
        assert any(step.get('uses') == './comment' for step in job['steps']), name


def test_the_fixture_publishes_one_check_run_from_the_report_job_that_holds_checks_write():
    job = REPORT_JOBS['pr-comment-report']
    reporting = [step for step in job['steps'] if step.get('uses') == './comment' and step['with'].get('test_report') != 'false']

    assert job['permissions']['checks'] == 'write'
    assert len(reporting) == 1
    assert 'checks' not in REPORT_JOBS['no-comment-report']['permissions']


def test_the_fixture_proves_the_matrix_refusal_with_two_legs_that_never_reach_the_stub():
    job = FIXTURE['jobs']['matrix']
    expect = next(step for step in job['steps'] if step.get('uses') == './.github/actions/expect')

    assert job['strategy']['matrix'] == {'leg': ['one', 'two']}
    assert job['strategy']['fail-fast'] is False
    assert expect['with'] == {'code': '65', 'heading': '⛔ refused: the job is one of 2 matrix legs', 'stub': 'false'}


def test_every_fixture_checkout_keeps_no_credential():
    checkouts = [
        step
        for job in FIXTURE['jobs'].values()
        for step in job['steps']
        if str(step.get('uses', '')).startswith('actions/checkout@')
    ]

    assert checkouts
    for step in checkouts:
        assert step['with']['persist-credentials'] is False


def test_no_description_holds_an_expression():
    """The runner evaluates `${{ }}` in an input's description, where `needs` and the like are not available."""
    for document in (ACTION, COMMENT):
        for name, spec in document['inputs'].items():
            assert '${{' not in spec.get('description', ''), name


def test_the_run_step_cannot_request_an_oidc_token():
    env = STEPS['run']['env']

    assert env['ACTIONS_ID_TOKEN_REQUEST_URL'] == ''
    assert env['ACTIONS_ID_TOKEN_REQUEST_TOKEN'] == ''


def test_no_step_after_the_plan_offers_an_oidc_token_request():
    steps = [name for name in ORDER if ORDER.index(name) > ORDER.index('plan')]

    assert {'install', 'run'} <= set(steps)
    for name in steps:
        env = STEPS[name].get('env', {})
        assert env.get('ACTIONS_ID_TOKEN_REQUEST_URL') == '', name
        assert env.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN') == '', name


def test_every_script_step_after_the_plan_runs_the_interpreter_the_plan_resolved():
    for name, step in STEPS.items():
        if name == 'plan' or 'run' not in step:
            continue
        command = step['run'].removeprefix('exec ')
        assert command.startswith('"$SA_PYTHON" '), name
        assert step['env']['SA_PYTHON'] == '${{ steps.plan.outputs.python }}', name


def test_every_script_step_runs_the_interpreter_without_site_packages_or_python_environment_variables():
    """``-S`` skips ``.pth`` files of the project's site-packages and ``-E`` ignores ``PYTHONPATH`` and ``PYTHONSTARTUP``."""
    for name, step in STEPS.items():
        if name == 'plan' or 'run' not in step:
            continue
        command = step['run'].removeprefix('exec ')
        assert command.startswith('"$SA_PYTHON" -S -E "$GITHUB_ACTION_PATH/scripts/'), name


def test_the_plan_step_runs_the_scripts_with_the_same_flags():
    assert STEPS['plan']['run'] == 'python3 -S -E "$GITHUB_ACTION_PATH/scripts/plan.py"'


def test_the_plan_step_tells_the_planner_whether_the_job_is_a_matrix_leg():
    assert STEPS['plan']['env']['SA_JOB_TOTAL'] == '${{ strategy.job-total }}'
    assert STEPS['plan']['env']['SA_JOB_INDEX'] == '${{ strategy.job-index }}'


def test_the_fetch_step_gets_the_baseline_branch_and_what_it_needs_to_read_the_visibility():
    env = STEPS['fetch']['env']

    assert env['SA_MAIN_BRANCH'] == '${{ steps.plan.outputs.main_branch }}'
    assert env['SA_VISIBILITY'] == '${{ steps.plan.outputs.visibility }}'
    assert env['SA_INCLUDE_CONTENT_SETTING'] == '${{ steps.plan.outputs.include_content_setting }}'


def test_the_visibility_and_the_content_decision_come_from_the_fetch_step():
    assert STEPS['run']['env']['SA_VISIBILITY'] == '${{ steps.fetch.outputs.visibility }}'
    assert STEPS['Upload transcripts']['if'] == "always() && steps.fetch.outputs.include_content == 'true'"


def test_the_summary_steps_know_which_step_before_the_run_failed():
    for name in ('Compose the comment body for the report job', 'Write the job summary'):
        env = STEPS[name]['env']
        assert env['SA_PLAN_OUTCOME'] == '${{ steps.plan.outcome }}', name
        assert env['SA_FETCH_OUTCOME'] == '${{ steps.fetch.outcome }}', name
        assert env['SA_INSTALL_OUTCOME'] == '${{ steps.install.outcome }}', name


def test_the_run_step_passes_the_ref_the_caller_used():
    assert STEPS['run']['env']['SA_ACTION_REF'] == '${{ github.action_ref }}'


ACTION_REF_WORKFLOW = yaml.safe_load((REPO / '.github' / 'workflows' / 'action-ref.yml').read_text())
ACTION_REF_JOB = ACTION_REF_WORKFLOW['jobs']['action-ref']
BRANCH = 'main'


def test_the_action_ref_check_runs_on_dispatch_and_pushes_to_main_only():
    triggers = ACTION_REF_WORKFLOW[True] if True in ACTION_REF_WORKFLOW else ACTION_REF_WORKFLOW['on']

    assert set(triggers) == {'push', 'workflow_dispatch'}
    assert triggers['push']['branches'] == [BRANCH]


def test_the_action_ref_check_has_no_secret_and_no_write_permission():
    assert ACTION_REF_WORKFLOW['permissions'] == {'contents': 'read', 'actions': 'read'}
    assert 'permissions' not in ACTION_REF_JOB
    assert 'secrets.' not in str(ACTION_REF_JOB)


def test_the_action_ref_check_runs_the_action_by_name_at_the_top_level_of_the_job():
    steps = [step for step in ACTION_REF_JOB['steps'] if str(step.get('uses', '')).startswith('plurai-ai/simulate-action@')]

    assert len(steps) == 1
    assert steps[0]['uses'] == f'plurai-ai/simulate-action@{BRANCH}'
    assert not [step for step in ACTION_REF_JOB['steps'] if '.github/actions/expect' in str(step.get('uses', ''))]


def test_the_action_ref_check_asserts_the_ref_it_runs():
    ref = ACTION_REF_JOB['env']['ACTION_REF']
    run = next(step for step in ACTION_REF_JOB['steps'] if 'assert_run.py' in step.get('run', ''))

    assert ref == BRANCH
    assert '--expect-action-version "$ACTION_REF"' in run['run']


def test_the_comment_steps_run_the_scripts_with_the_same_flags():
    for name, step in COMMENT_STEPS.items():
        if 'run' in step:
            assert step['run'].startswith('python3 -S -E "$GITHUB_ACTION_PATH/../scripts/'), name


def test_the_fixture_helper_retries_a_download_that_is_expected_to_find_its_artifact():
    """The artifacts API can answer 404 for an artifact finalized a moment earlier; a helper that gives up on the first
    answer fails a correct run."""
    helper = yaml.safe_load((REPO / '.github' / 'actions' / 'expect' / 'action.yml').read_text())
    step = next(step for step in helper['runs']['steps'] if step['name'] == 'Download what the action uploaded')

    assert step['env']['EXPECT_REPORT'] == '${{ inputs.stub }}'
    assert step['env']['EXPECT_TRANSCRIPTS'] == '${{ inputs.expect_transcripts }}'
    assert 'sleep' in step['run']
    assert 'attempts "$EXPECT_REPORT" true' in step['run']
    assert 'attempts "$EXPECT_TRANSCRIPTS" present' in step['run']
