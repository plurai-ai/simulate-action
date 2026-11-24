"""comment_plan.py: whether the ``report`` job comments, and which artifact and sticky comment are this workflow job's."""

from __future__ import annotations

import pytest
from conftest import SHA_A, SHA_B, write_event

WORKFLOW_REF = 'acme/agents/.github/workflows/plurai-support.yml@refs/heads/feature'


def pull_request(head_repo='acme/agents', number=7):
    return {'pull_request': {'number': number, 'base': {'sha': SHA_A}, 'head': {'sha': SHA_B, 'repo': {'full_name': head_repo}}}}


@pytest.fixture
def plan(run_script, tmp_path):
    def run(event_name='pull_request', payload=None, **env):
        full = {
            'GITHUB_EVENT_NAME': event_name,
            'GITHUB_EVENT_PATH': write_event(tmp_path, pull_request() if payload is None else payload),
            'GITHUB_REPOSITORY': 'acme/agents',
            'GITHUB_ACTOR': 'octocat',
            'GITHUB_WORKFLOW_REF': WORKFLOW_REF,
            'GITHUB_JOB': 'report',
            'INPUT_JOB': 'simulate',
            'INPUT_PR_COMMENT': 'true',
            'INPUT_TEST_REPORT': 'true',
            'INPUT_CHECK_NAME': '',
            'GITHUB_WORKFLOW': 'Plurai simulation (support)',
            'SA_RESULT': 'success',
            **env,
        }
        result = run_script('comment_plan.py', full)
        summary = tmp_path / 'github_step_summary'
        result.summary = summary.read_text() if summary.exists() else ''
        return result

    return run


def test_a_same_repo_pull_request_comments(plan):
    result = plan()

    assert result.code == 0
    assert result.outputs['comment_mode'] == 'on'
    assert result.outputs['pr_number'] == '7'


def test_the_artifact_and_the_marker_are_those_of_the_simulate_job_not_the_report_job(plan):
    result = plan()

    assert result.outputs['workflow_key'] == 'plurai-support-simulate'
    assert result.outputs['artifact'] == 'plurai-plurai-support-simulate-report'


def test_a_fork_pr_skips_the_comment_and_the_report_with_one_note(plan):
    result = plan(payload=pull_request(head_repo='someone/agents'))

    assert result.outputs['comment_mode'] == 'skip'
    assert result.outputs['report_mode'] == 'skip'
    assert result.summary.count('fork') == 1
    assert 'PR comment and test report skipped' in result.summary


def test_a_dependabot_pr_skips_the_comment_and_the_report_with_a_note(plan):
    result = plan(GITHUB_ACTOR='dependabot[bot]')

    assert result.outputs['comment_mode'] == 'skip'
    assert result.outputs['report_mode'] == 'skip'
    assert 'Dependabot' in result.summary


def test_pr_comment_false_turns_the_comment_off_without_a_note(plan):
    result = plan(INPUT_PR_COMMENT='false')

    assert result.outputs['comment_mode'] == 'off'
    assert result.summary == ''


@pytest.mark.parametrize('event', ['push', 'schedule', 'workflow_dispatch', 'merge_group'])
def test_events_other_than_pull_request_make_no_comment(plan, event):
    assert plan(event_name=event, payload={}).outputs['comment_mode'] == 'off'


def test_a_skipped_simulate_job_makes_no_comment(plan):
    assert plan(SA_RESULT='skipped').outputs['comment_mode'] == 'off'


def test_the_simulate_job_id_is_required(plan):
    result = plan(INPUT_JOB='  ')

    assert result.code == 65
    assert '`job`' in result.stderr


def test_the_head_sha_of_the_run_is_the_pull_requests_head_in_the_event(plan):
    assert plan().outputs['head_sha'] == SHA_B


def test_a_same_repo_pull_request_reports(plan):
    assert plan().outputs['report_mode'] == 'on'


@pytest.mark.parametrize('event', ['push', 'schedule', 'workflow_dispatch', 'merge_group'])
def test_every_event_that_ran_simulate_reports_without_a_comment(plan, event):
    result = plan(event_name=event, payload={})

    assert result.outputs['report_mode'] == 'on'
    assert result.outputs['comment_mode'] == 'off'
    assert result.summary == ''


def test_a_skipped_simulate_job_makes_no_report(plan):
    assert plan(SA_RESULT='skipped').outputs['report_mode'] == 'off'


def test_test_report_false_turns_the_report_off_and_keeps_the_comment(plan):
    result = plan(INPUT_TEST_REPORT='false')

    assert result.outputs['report_mode'] == 'off'
    assert result.outputs['comment_mode'] == 'on'
    assert result.summary == ''


def test_pr_comment_false_on_a_fork_notes_only_the_report(plan):
    result = plan(payload=pull_request(head_repo='someone/agents'), INPUT_PR_COMMENT='false')

    assert result.outputs['report_mode'] == 'skip'
    assert 'PR comment' not in result.summary
    assert 'test report skipped' in result.summary


def test_a_fork_run_with_nothing_wanted_leaves_no_note(plan):
    result = plan(payload=pull_request(head_repo='someone/agents'), INPUT_PR_COMMENT='false', INPUT_TEST_REPORT='false')

    assert result.summary == ''


def test_the_check_name_defaults_to_the_workflow_name_and_cannot_equal_the_simulate_check(plan):
    name = plan().outputs['check_name']

    assert name == 'Plurai simulation results (support)'
    assert name != 'Plurai simulation (support)'


def test_a_workflow_not_named_by_the_template_gets_its_name_in_the_check_name(plan):
    assert plan(GITHUB_WORKFLOW='Nightly agent').outputs['check_name'] == 'Plurai simulation results (Nightly agent)'


def test_the_check_name_input_wins(plan):
    assert (
        plan(INPUT_CHECK_NAME='Plurai simulation results (support)').outputs['check_name']
        == 'Plurai simulation results (support)'
    )
