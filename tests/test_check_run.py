"""check_run.py: the check run that shows the report, created with the job token from the report the run job uploaded."""

from __future__ import annotations

import os
import re

import pytest
from fake_github import drop, garbage, stall

REPO = 'acme/agents'
HEAD = 'a' * 40
RUN_SHA = 'c' * 40
CHECK_NAME = 'Plurai simulation results (support)'
PASSED = '## Plurai simulation · ✅ 0 failed · 0 unstable · 0 advisory · 0 not measured'
FAILED = '## Plurai simulation · ❌ 2 failed — refund_flow / no_refund_over_limit · 0 not measured'
NOT_MEASURED = '## Plurai simulation · ⛔ not measured: infra · 0 failed'


@pytest.fixture
def check(run_script, github, tmp_path, out_dir):
    github.route('POST', rf'/repos/{REPO}/check-runs', lambda _r, _m: (201, {'id': 9, 'html_url': f'{github.url}/runs/9'}))

    def run(report: str | None = f'{PASSED}\n\nResults.\n', mode='on', **env):
        if report is not None:
            (out_dir / 'report.md').write_text(report)
        full = {
            'SA_REPORT_DIR': str(out_dir),
            'SA_REPORT_MODE': mode,
            'SA_CHECK_NAME': CHECK_NAME,
            'SA_HEAD_SHA': HEAD,
            'GITHUB_SHA': RUN_SHA,
            'GITHUB_API_URL': github.url,
            'GITHUB_TOKEN': 'job-token',
            'GITHUB_REPOSITORY': REPO,
            'GITHUB_SERVER_URL': 'https://github.example',
            'GITHUB_RUN_ID': '77',
            **env,
        }
        result = run_script('check_run.py', full)
        summary = tmp_path / 'github_step_summary'
        result.summary = summary.read_text() if summary.exists() else ''
        return result

    return run


def created(github) -> dict:
    calls = github.calls('POST', rf'/repos/{REPO}/check-runs')
    assert len(calls) == 1
    return calls[0].body


def test_a_pull_request_run_creates_one_completed_check_on_the_pr_head(check, github):
    result = check()

    body = created(github)
    assert result.code == 0
    assert body['name'] == CHECK_NAME
    assert body['head_sha'] == HEAD
    assert body['status'] == 'completed'
    assert body['details_url'] == 'https://github.example/acme/agents/actions/runs/77'
    assert result.outputs['check_url'] == f'{github.url}/runs/9'


def test_the_job_token_is_the_bearer_credential(check, github):
    check()

    assert github.calls('POST', rf'/repos/{REPO}/check-runs')[0].headers['authorization'] == 'Bearer job-token'


@pytest.mark.parametrize('event_sha', ['', None])
def test_an_event_without_a_pr_head_uses_the_commit_the_run_ran_on(check, github, event_sha):
    check(SA_HEAD_SHA=event_sha or '')

    assert created(github)['head_sha'] == RUN_SHA


def test_the_title_is_the_reports_counts_line_and_the_summary_is_the_rest_of_the_report(check, github):
    check(report=f'{FAILED}\n\n### Failed\n\n| a | b |\n')

    output = created(github)['output']
    assert output['title'] == '❌ 2 failed — refund_flow / no_refund_over_limit · 0 not measured'
    assert output['summary'].startswith('### Failed')
    assert '| a | b |' in output['summary']
    assert not output['summary'].startswith('## ')


@pytest.mark.parametrize(
    ('heading', 'conclusion'),
    [(PASSED, 'success'), (FAILED, 'failure'), (NOT_MEASURED, 'neutral'), ('## Plurai simulation · something new', 'neutral')],
)
def test_the_conclusion_follows_the_verdict_mark_of_the_heading(check, github, heading, conclusion):
    check(report=f'{heading}\n\nBody.\n')

    assert created(github)['conclusion'] == conclusion


def test_a_report_with_only_a_heading_has_the_heading_as_its_summary(check, github):
    check(report=f'{PASSED}\n')

    assert created(github)['output']['summary'] == created(github)['output']['title']


def test_a_long_report_is_cut_under_the_limit_with_a_pointer_to_the_job_summary(check, github):
    check(report=f'{PASSED}\n\n' + 'x' * 200_000)

    summary = created(github)['output']['summary']
    assert len(summary) <= 65_535
    assert 'job summary' in summary
    assert 'https://github.example/acme/agents/actions/runs/77' in summary


def test_a_long_title_is_cut(check, github):
    check(report='## Plurai simulation · ❌ ' + 'y' * 1000 + '\n')

    assert len(created(github)['output']['title']) <= 255


@pytest.mark.parametrize('event', ['push', 'schedule', 'workflow_dispatch', 'merge_group'])
def test_events_other_than_pull_request_create_a_check_too(check, github, event):
    check(SA_HEAD_SHA='', GITHUB_EVENT_NAME=event)

    assert created(github)['head_sha'] == RUN_SHA


@pytest.mark.parametrize('mode', ['off', 'skip'])
def test_a_report_mode_other_than_on_calls_nothing(check, github, mode):
    result = check(mode=mode)

    assert result.code == 0
    assert github.requests == []
    assert result.outputs['check_url'] == ''


def test_a_missing_report_creates_no_check_and_does_not_fail(check, github, out_dir):
    result = check(report=None)

    assert result.code == 0
    assert github.requests == []
    assert 'no check' in result.stdout


def test_a_report_directory_that_was_never_downloaded_leaves_the_note_and_no_check(check, github, tmp_path):
    """The download step fails when the artifact is missing, and then its target directory does not exist."""
    result = check(report=None, SA_REPORT_DIR=str(tmp_path / 'never-downloaded'))

    assert result.code == 0
    assert github.requests == []
    assert 'no check run: the simulate job left no report.md' in result.stdout
    assert result.outputs['check_url'] == ''


def test_a_report_that_is_a_symlink_is_not_read(check, github, out_dir, tmp_path):
    (tmp_path / 'secret.md').write_text(f'{PASSED}\n\nsecret\n')
    (out_dir / 'report.md').symlink_to(tmp_path / 'secret.md')

    result = check(report=None)

    assert result.code == 0
    assert github.requests == []


def test_a_token_without_checks_write_skips_with_a_note_and_does_not_fail(check, github):
    github.routes.clear()
    github.route('POST', rf'/repos/{REPO}/check-runs', {'message': 'Resource not accessible by integration'}, status=403)

    result = check()

    assert result.code == 0
    assert 'checks: write' in result.summary
    assert result.outputs['check_url'] == ''


def test_another_api_error_is_a_warning_and_a_note_and_does_not_fail(check, github):
    github.routes.clear()
    github.route('POST', rf'/repos/{REPO}/check-runs', {'message': 'boom'}, status=500)

    result = check()

    assert result.code == 0
    assert re.search(r'::warning::.*not created', result.stdout)
    assert 'not created' in result.summary


TRANSPORT_FAILURES = {'disconnect': drop, 'timeout': stall(2), 'bad-json': garbage}


@pytest.mark.parametrize('failure', sorted(TRANSPORT_FAILURES))
def test_a_transport_error_creating_the_check_is_a_note_and_never_fails_the_job(check, github, failure):
    github.routes.clear()
    github.route('POST', rf'/repos/{REPO}/check-runs', TRANSPORT_FAILURES[failure])

    result = check(SA_API_TIMEOUT_SECONDS='0.3')

    assert result.code == 0
    assert result.outputs['check_url'] == ''
    assert 'Test report check not created' in result.summary
    assert '::warning::' in result.stdout
    assert 'Traceback' not in result.stderr


def test_a_reply_without_a_url_is_a_note_and_never_fails_the_job(check, github):
    github.routes.clear()
    github.route('POST', rf'/repos/{REPO}/check-runs', {'id': 9})

    result = check()

    assert result.code == 0
    assert result.outputs['check_url'] == ''


@pytest.mark.skipif(os.geteuid() == 0, reason='root reads any file')
def test_a_report_that_cannot_be_read_is_a_warning_and_never_fails_the_job(check, github, out_dir):
    report = out_dir / 'report.md'
    report.write_text(f'{PASSED}\n')
    report.chmod(0)

    result = check(report=None)

    assert result.code == 0
    assert re.search(r'::warning::.*check run was not created', result.stdout)
    assert 'Traceback' not in result.stderr
    assert result.outputs['check_url'] == ''
    assert github.requests == []


def test_a_missing_check_name_is_a_warning_and_never_fails_the_job(run_script, out_dir):
    (out_dir / 'report.md').write_text(f'{PASSED}\n')

    result = run_script(
        'check_run.py',
        {'SA_REPORT_DIR': str(out_dir), 'SA_REPORT_MODE': 'on', 'GITHUB_REPOSITORY': REPO, 'GITHUB_SHA': RUN_SHA},
    )

    assert result.code == 0
    assert re.search(r'::warning::.*check run was not created', result.stdout)
    assert 'Traceback' not in result.stderr
    assert result.outputs['check_url'] == ''


@pytest.mark.parametrize('reply', [None, [], 'text'])
def test_an_unexpected_reply_to_the_create_call_is_a_note_and_never_fails_the_job(check, github, reply):
    github.routes.clear()
    github.route('POST', rf'/repos/{REPO}/check-runs', reply)

    result = check()

    assert result.code == 0
    assert result.outputs['check_url'] == ''
    assert 'Traceback' not in result.stderr
    assert '::warning::' in result.stdout
