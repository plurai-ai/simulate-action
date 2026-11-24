"""comment.py: the sticky PR comment, one per PR and workflow, edited in place, from the report the run job uploaded."""

from __future__ import annotations

import os
import re

import pytest
from fake_github import drop, garbage, stall

REPO = 'acme/agents'
BOT = {'login': 'github-actions[bot]', 'type': 'Bot'}
MARKER = '<!-- plurai-simulation-report -->'
HEAD = 'a' * 40
NEWER = 'b' * 40
KEY_MARKER = '<!-- plurai-simulation-report:workflow=plurai-support -->'


@pytest.fixture
def comment(run_script, github, tmp_path, out_dir):
    github.route('GET', rf'/repos/{REPO}/issues/7/comments', [])
    github.route('POST', rf'/repos/{REPO}/issues/7/comments', lambda _r, _m: (201, {'id': 1, 'html_url': f'{github.url}/c/1'}))

    def run(body: str | None = f'{MARKER}\n## Plurai simulation · ✅ ok\n', mode='on', **env):
        if body is not None:
            (out_dir / 'comment-final.md').write_text(body)
        full = {
            'SA_REPORT_DIR': str(out_dir),
            'SA_HEAD_SHA': HEAD,
            'SA_RESULT': 'success',
            'GITHUB_API_URL': github.url,
            'GITHUB_TOKEN': 'job-token',
            'GITHUB_REPOSITORY': REPO,
            'SA_PR_NUMBER': '7',
            'SA_COMMENT_MODE': mode,
            'SA_WORKFLOW_KEY': 'plurai-support',
            **env,
        }
        return run_script('comment.py', full)

    return run


def existing(comment_id: int, body: str, user: dict = BOT) -> dict:
    return {'id': comment_id, 'body': body, 'user': user}


def test_posts_a_new_comment_when_none_exists(comment, github):
    result = comment()

    assert result.code == 0
    posted = github.calls('POST', rf'/repos/{REPO}/issues/7/comments')[0].body['body']
    assert posted.startswith(f'{MARKER}\n{KEY_MARKER}\n')
    assert '✅ ok' in posted
    assert result.outputs['comment_url'] == f'{github.url}/c/1'


def test_a_second_run_edits_the_existing_comment_in_place(comment, github):
    github.routes.clear()
    github.route('GET', rf'/repos/{REPO}/issues/7/comments', [existing(41, f'{MARKER}\n{KEY_MARKER}\nold')])
    github.route('PATCH', rf'/repos/{REPO}/issues/comments/41', lambda _r, _m: (200, {'id': 41, 'html_url': 'u'}))

    comment()

    assert len(github.calls('PATCH', rf'/repos/{REPO}/issues/comments/41')) == 1
    assert github.calls('POST', rf'/repos/{REPO}/issues/7/comments') == []


def test_another_workflows_comment_is_left_alone(comment, github):
    github.routes.clear()
    other = '<!-- plurai-simulation-report:workflow=plurai-billing -->'
    github.route('GET', rf'/repos/{REPO}/issues/7/comments', [existing(41, f'{MARKER}\n{other}\nbilling')])
    github.route('POST', rf'/repos/{REPO}/issues/7/comments', lambda _r, _m: (201, {'id': 2, 'html_url': 'u'}))

    comment()

    assert github.calls('PATCH', r'.*') == []
    assert len(github.calls('POST', rf'/repos/{REPO}/issues/7/comments')) == 1


def test_a_marker_from_another_author_is_not_ours(comment, github):
    github.routes.clear()
    github.route(
        'GET',
        rf'/repos/{REPO}/issues/7/comments',
        [existing(41, f'{MARKER}\n{KEY_MARKER}\nplanted', {'login': 'mallory', 'type': 'User'})],
    )
    github.route('POST', rf'/repos/{REPO}/issues/7/comments', lambda _r, _m: (201, {'id': 2, 'html_url': 'u'}))

    comment()

    assert github.calls('PATCH', r'.*') == []


def test_finds_the_comment_past_the_first_page(comment, github):
    github.routes.clear()

    def page(request, _match):
        if request.query.endswith('&page=1'):
            return 200, [existing(i, 'chatter', {'login': 'human', 'type': 'User'}) for i in range(100)]
        return 200, [existing(500, f'{MARKER}\n{KEY_MARKER}\nold')]

    github.route('GET', rf'/repos/{REPO}/issues/7/comments', page)
    github.route('PATCH', rf'/repos/{REPO}/issues/comments/500', lambda _r, _m: (200, {'id': 500, 'html_url': 'u'}))

    comment()

    assert len(github.calls('PATCH', rf'/repos/{REPO}/issues/comments/500')) == 1


@pytest.mark.parametrize('mode', ['off', 'skip'])
def test_off_and_skip_make_no_request(comment, github, mode):
    result = comment(mode=mode)

    assert result.code == 0
    assert github.requests == []


@pytest.mark.parametrize('status', [403, 404])
def test_no_write_permission_is_a_note_and_never_a_failure(comment, github, tmp_path, status):
    github.routes.clear()
    github.route('GET', rf'/repos/{REPO}/issues/7/comments', [])
    github.route(
        'POST', rf'/repos/{REPO}/issues/7/comments', {'message': 'Resource not accessible by integration'}, status=status
    )

    result = comment()

    assert result.code == 0
    assert 'pull-requests: write' in (tmp_path / 'github_step_summary').read_text()


def test_a_server_error_is_a_warning_and_never_a_failure(comment, github):
    github.routes.clear()
    github.route('GET', rf'/repos/{REPO}/issues/7/comments', {'message': 'boom'}, status=500)

    result = comment()

    assert result.code == 0
    assert '::warning::' in result.stdout


def test_a_long_body_is_cut_to_the_comment_limit_with_a_pointer(comment, github):
    comment(body=f'{MARKER}\n' + 'x' * 70_000)

    posted = github.calls('POST', rf'/repos/{REPO}/issues/7/comments')[0].body['body']
    assert len(posted) <= 65_536
    assert 'report artifact' in posted
    assert posted.startswith(MARKER)


def test_a_body_without_the_marker_gets_it(comment, github):
    comment(body='## bare heading\n')

    posted = github.calls('POST', rf'/repos/{REPO}/issues/7/comments')[0].body['body']
    assert posted.startswith(f'{MARKER}\n{KEY_MARKER}\n{SHA_MARKER}\n## bare')


def test_the_token_never_reaches_the_log(comment):
    result = comment()

    assert 'job-token' not in result.stdout + result.stderr


def test_a_run_that_uploaded_no_report_gets_a_comment_naming_its_result(comment, github, out_dir):
    result = comment(body=None, SA_RESULT='cancelled')

    assert result.code == 0
    posted = github.calls('POST', rf'/repos/{REPO}/issues/7/comments')[0].body['body']
    assert posted.startswith(f'{MARKER}\n{KEY_MARKER}\n{SHA_MARKER}\n## Plurai simulation · ⛔ not measured')
    assert 'cancelled' in posted


def test_a_report_that_is_a_symlink_is_not_read(comment, github, out_dir, tmp_path):
    secret = tmp_path / 'secret.txt'
    secret.write_text('runner secret')
    (out_dir / 'comment-final.md').symlink_to(secret)

    comment(body=None)

    posted = github.calls('POST', rf'/repos/{REPO}/issues/7/comments')[0].body['body']
    assert 'runner secret' not in posted
    assert 'no report' in posted


def test_an_oversized_report_is_cut_before_it_is_held_in_memory(comment, github):
    comment(body=f'{MARKER}\n' + 'x' * 5_000_000)

    posted = github.calls('POST', rf'/repos/{REPO}/issues/7/comments')[0].body['body']
    assert len(posted) <= 65_536


def test_a_marker_quoted_further_down_a_comment_does_not_make_it_ours(comment, github):
    github.routes.clear()
    quoting = f'{MARKER}\n<!-- plurai-simulation-report:workflow=plurai-billing -->\ntext\n{KEY_MARKER}\n'
    github.route('GET', rf'/repos/{REPO}/issues/7/comments', [existing(41, quoting)])
    github.route('POST', rf'/repos/{REPO}/issues/7/comments', lambda _r, _m: (201, {'id': 2, 'html_url': 'u'}))

    comment()

    assert github.calls('PATCH', r'.*') == []
    assert len(github.calls('POST', rf'/repos/{REPO}/issues/7/comments')) == 1


SHA_MARKER = f'<!-- plurai-simulation-report:sha={HEAD} -->'


def pr_head(github, sha):
    github.route('GET', rf'/repos/{REPO}/pulls/7', {'number': 7, 'head': {'sha': sha}})


def test_the_comment_records_the_head_sha_of_its_run_on_a_third_marker_line(comment, github):
    pr_head(github, HEAD)

    comment()

    posted = github.calls('POST', rf'/repos/{REPO}/issues/7/comments')[0].body['body']
    assert posted.splitlines()[:3] == [MARKER, KEY_MARKER, SHA_MARKER]


def test_a_run_that_is_no_longer_the_latest_head_leaves_the_comment_alone(comment, github):
    pr_head(github, NEWER)

    result = comment()

    assert result.code == 0
    assert github.calls('POST', rf'/repos/{REPO}/issues/7/comments') == []
    assert github.calls('PATCH', r'.*') == []
    assert 'no longer the head' in result.stdout


def _existing_with_sha(github, sha):
    github.routes[:] = [r for r in github.routes if r[0] != 'GET' or 'comments' not in r[1].pattern]
    marker = f'<!-- plurai-simulation-report:sha={sha} -->'
    github.route('GET', rf'/repos/{REPO}/issues/7/comments', [existing(41, f'{MARKER}\n{KEY_MARKER}\n{marker}\nolder')])
    github.route('PATCH', rf'/repos/{REPO}/issues/comments/41', lambda _r, _m: (200, {'id': 41, 'html_url': 'u'}))


def test_a_cancelled_run_does_not_replace_a_comment_of_another_head_when_the_pr_head_is_unknown(comment, github):
    _existing_with_sha(github, NEWER)

    result = comment(SA_RESULT='cancelled')

    assert result.code == 0
    assert github.calls('PATCH', r'.*') == []


def test_a_cancelled_run_on_the_current_head_replaces_the_comment(comment, github):
    pr_head(github, HEAD)
    _existing_with_sha(github, NEWER)

    comment(SA_RESULT='cancelled')

    assert len(github.calls('PATCH', rf'/repos/{REPO}/issues/comments/41')) == 1


def test_a_cancelled_run_edits_a_comment_that_has_no_sha_marker(comment, github):
    github.routes[:] = [r for r in github.routes if r[0] != 'GET' or 'comments' not in r[1].pattern]
    github.route('GET', rf'/repos/{REPO}/issues/7/comments', [existing(41, f'{MARKER}\n{KEY_MARKER}\nold')])
    github.route('PATCH', rf'/repos/{REPO}/issues/comments/41', lambda _r, _m: (200, {'id': 41, 'html_url': 'u'}))

    comment(SA_RESULT='cancelled')

    assert len(github.calls('PATCH', rf'/repos/{REPO}/issues/comments/41')) == 1


#: The transport failures below the HTTP status: an unanswered connection, a stall past the client's timeout, a body that is
#: not JSON.
TRANSPORT_FAILURES = {'disconnect': drop, 'timeout': stall(2), 'bad-json': garbage}
FAST = {'SA_API_TIMEOUT_SECONDS': '0.3'}


@pytest.fixture(params=sorted(TRANSPORT_FAILURES))
def broken(request):
    return TRANSPORT_FAILURES[request.param]


def test_a_transport_error_listing_the_comments_is_a_warning_and_never_fails_the_job(comment, github, broken):
    github.routes.clear()
    github.route('GET', rf'/repos/{REPO}/issues/7/comments', broken)

    result = comment(**FAST)

    assert result.code == 0
    assert result.outputs['comment_url'] == ''
    assert '::warning::' in result.stdout
    assert 'Traceback' not in result.stderr


def test_a_transport_error_posting_the_comment_is_a_warning_and_never_fails_the_job(comment, github, broken):
    github.routes.clear()
    github.route('GET', rf'/repos/{REPO}/issues/7/comments', [])
    github.route('POST', rf'/repos/{REPO}/issues/7/comments', broken)

    result = comment(**FAST)

    assert result.code == 0
    assert result.outputs['comment_url'] == ''
    assert '::warning::' in result.stdout


def test_a_transport_error_reading_the_pr_head_does_not_stop_the_comment(comment, github, broken):
    github.route('GET', rf'/repos/{REPO}/pulls/7', broken)

    result = comment(**FAST)

    assert result.code == 0
    assert github.calls('POST', rf'/repos/{REPO}/issues/7/comments')


def test_a_reply_without_a_url_is_a_warning_and_never_fails_the_job(comment, github):
    github.routes.clear()
    github.route('GET', rf'/repos/{REPO}/issues/7/comments', [])
    github.route('POST', rf'/repos/{REPO}/issues/7/comments', {'id': 1})

    result = comment()

    assert result.code == 0
    assert result.outputs['comment_url'] == ''


def test_a_missing_report_artifact_leaves_the_existing_comment_alone(comment, github, out_dir):
    github.routes.clear()
    github.route('GET', rf'/repos/{REPO}/issues/7/comments', [existing(41, f'{MARKER}\n{KEY_MARKER}\n## a good report')])
    github.route('PATCH', rf'/repos/{REPO}/issues/comments/41', lambda _r, _m: (200, {'id': 41, 'html_url': 'u'}))

    result = comment(body=None, SA_RESULT='success')

    assert result.code == 0
    assert github.calls('PATCH', r'.*') == []
    assert github.calls('POST', r'.*') == []
    assert 'left as it is' in (out_dir.parent / 'github_step_summary').read_text()


def test_a_missing_report_artifact_leaves_the_existing_comment_alone_for_a_cancelled_run_too(comment, github):
    github.routes.clear()
    github.route('GET', rf'/repos/{REPO}/issues/7/comments', [existing(41, f'{MARKER}\n{KEY_MARKER}\n## a good report')])
    github.route('PATCH', rf'/repos/{REPO}/issues/comments/41', lambda _r, _m: (200, {'id': 41, 'html_url': 'u'}))

    comment(body=None, SA_RESULT='cancelled')

    assert github.calls('PATCH', r'.*') == []


def test_a_missing_report_artifact_with_no_comment_yet_posts_an_honest_one(comment, github):
    comment(body=None, SA_RESULT='failure')

    posted = github.calls('POST', rf'/repos/{REPO}/issues/7/comments')[0].body['body']
    assert '⛔ no report: the simulate job left no report artifact' in posted
    assert '`failure`' in posted
    assert 'ended without writing' not in posted


def test_a_missing_report_artifact_of_a_successful_job_does_not_blame_plurai_ci_run(comment, github):
    comment(body=None, SA_RESULT='success')

    posted = github.calls('POST', rf'/repos/{REPO}/issues/7/comments')[0].body['body']
    assert 'ended without writing' not in posted
    assert '`success`' in posted


def test_comment_items_that_are_not_objects_are_skipped(comment, github):
    github.routes.clear()
    github.route('GET', rf'/repos/{REPO}/issues/7/comments', [None, 'text', 3, existing(41, f'{MARKER}\n{KEY_MARKER}\nold')])
    github.route('PATCH', rf'/repos/{REPO}/issues/comments/41', lambda _r, _m: (200, {'id': 41, 'html_url': 'u'}))

    result = comment()

    assert result.code == 0
    assert len(github.calls('PATCH', rf'/repos/{REPO}/issues/comments/41')) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason='root reads any file')
def test_a_report_that_cannot_be_read_is_a_warning_and_never_fails_the_job(comment, github, out_dir):
    report = out_dir / 'comment-final.md'
    report.write_text(f'{MARKER}\nbody\n')
    report.chmod(0)

    result = comment(body=None)

    assert result.code == 0
    assert re.search(r'::warning::.*PR comment was not posted', result.stdout)
    assert 'Traceback' not in result.stderr
    assert result.outputs['comment_url'] == ''
    assert github.calls('POST', rf'/repos/{REPO}/issues/7/comments') == []


def test_a_missing_input_is_a_warning_and_never_fails_the_job(run_script):
    result = run_script('comment.py', {'SA_COMMENT_MODE': 'on'})

    assert result.code == 0
    assert re.search(r'::warning::.*PR comment was not posted', result.stdout)
    assert 'Traceback' not in result.stderr
    assert result.outputs['comment_url'] == ''


@pytest.mark.parametrize('shape', [None, 'text', 3])
def test_a_comment_listing_in_an_unexpected_shape_never_fails_the_job(comment, github, shape):
    github.routes.clear()
    github.route('GET', rf'/repos/{REPO}/issues/7/comments', shape)

    result = comment()

    assert result.code == 0
    assert 'Traceback' not in result.stderr
