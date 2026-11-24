"""fetch_context.py: main's latest run.json and the base commit of the suite diff, read with the job's token."""

from __future__ import annotations

import base64
import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from conftest import SHA_A, SHA_B, SHA_C
from fake_github import drop, garbage, stall

REPO = 'acme/agents'
WORKFLOW_REF = f'{REPO}/.github/workflows/plurai-support.yml@refs/heads/main'
ARTIFACT_NAME = 'plurai-plurai-support-report'


def zip_of(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def run_row(
    run_id: int, sha: str, conclusion: str = 'success', event: str = 'push', head_repo: str | None = REPO, created: str = ''
) -> dict:
    return {
        'id': run_id,
        'head_sha': sha,
        'conclusion': conclusion,
        'status': 'completed',
        'event': event,
        'head_repository': {'full_name': head_repo} if head_repo else None,
        'created_at': created or f'2026-10-{run_id // 100:02d}T00:00:00Z',
    }


@pytest.fixture
def fetch(run_script, github, tmp_path):
    def run(base_strategy='none', base='', pr_base='', pr_head='', run_id='900', **extra):
        env = {
            'GITHUB_API_URL': github.url,
            'GITHUB_TOKEN': 'job-token',
            'GITHUB_REPOSITORY': REPO,
            'GITHUB_RUN_ID': run_id,
            'GITHUB_WORKFLOW_REF': WORKFLOW_REF,
            'GITHUB_WORKSPACE': str(tmp_path / 'workspace'),
            # a server that does not exist: no test reaches github.com
            'GITHUB_SERVER_URL': f'file://{tmp_path / "no-server"}',
            'SA_BASE_STRATEGY': base_strategy,
            'SA_BASE': base,
            'SA_PR_BASE_SHA': pr_base,
            'SA_PR_HEAD_SHA': pr_head,
            'SA_WORKFLOW_KEY': 'plurai-support',
            'SA_DEFAULT_BRANCH': 'main',
            **extra,
        }
        (tmp_path / 'workspace').mkdir(exist_ok=True)
        return run_script('fetch_context.py', env)

    return run


RUNS = rf'/repos/{REPO}/actions/workflows/plurai-support\.yml/runs'


def list_runs(github, rows):
    """Serve ``rows`` the way the API does: only the runs of the queried ``event``."""

    def serve(request, _match):
        events = dict(pair.split('=', 1) for pair in request.query.split('&') if '=' in pair).get('event')
        return 200, {'workflow_runs': [row for row in rows if events is None or row['event'] == events]}

    github.route('GET', RUNS, serve)


def serve_artifact(github, run_id: int, artifact_id: int, files: dict[str, str], expired=False):
    github.route(
        'GET',
        rf'/repos/{REPO}/actions/runs/{run_id}/artifacts',
        {'artifacts': [{'id': artifact_id, 'name': ARTIFACT_NAME, 'expired': expired}]},
    )
    github.route('GET', rf'/repos/{REPO}/actions/artifacts/{artifact_id}/zip', zip_of(files))


def test_downloads_the_latest_completed_main_run_json(fetch, github, tmp_path):
    list_runs(github, [run_row(900, SHA_C), run_row(800, SHA_B), run_row(700, SHA_A)])
    serve_artifact(github, 800, 55, {'run.json': '{"schema": "plurai-run/1"}', 'report.md': 'x'})

    result = fetch()

    assert result.code == 0
    path = Path(result.outputs['main_run'])
    assert path.read_text() == '{"schema": "plurai-run/1"}'
    assert result.outputs['main_sha'] == SHA_B
    assert 'branch=main' in github.calls('GET', rf'/repos/{REPO}/actions/workflows/plurai-support\.yml/runs')[0].query


def test_the_current_run_is_never_its_own_main_run(fetch, github):
    list_runs(github, [run_row(900, SHA_C)])
    serve_artifact(github, 900, 1, {'run.json': '{}'})

    result = fetch()

    assert result.outputs['main_run'] == ''


def test_skips_runs_without_a_usable_artifact(fetch, github):
    list_runs(github, [run_row(800, SHA_B), run_row(700, SHA_A, conclusion='cancelled'), run_row(600, SHA_A)])
    serve_artifact(github, 800, 5, {'run.json': '{"gone": true}'}, expired=True)
    serve_artifact(github, 600, 6, {'run.json': '{"ok": true}'})

    result = fetch()

    assert Path(result.outputs['main_run']).read_text() == '{"ok": true}'
    assert result.outputs['main_sha'] == SHA_A


def test_none_found_omits_the_main_run(fetch, github, out_dir):
    list_runs(github, [])

    result = fetch()

    assert result.code == 0
    assert result.outputs['main_run'] == ''


def test_the_artifact_download_does_not_send_the_token_to_the_redirect_target(fetch, github):
    list_runs(github, [run_row(800, SHA_B)])
    github.route(
        'GET', rf'/repos/{REPO}/actions/runs/800/artifacts', {'artifacts': [{'id': 9, 'name': ARTIFACT_NAME, 'expired': False}]}
    )
    github.route(
        'GET', rf'/repos/{REPO}/actions/artifacts/9/zip', lambda _r, _m: (302, b'', {'Location': f'{github.url}/blob/9'})
    )
    github.route('GET', '/blob/9', zip_of({'run.json': '{"via": "redirect"}'}))

    result = fetch()

    assert Path(result.outputs['main_run']).read_text() == '{"via": "redirect"}'
    assert github.calls('GET', rf'/repos/{REPO}/actions/artifacts/9/zip')[0].headers['authorization'] == 'Bearer job-token'
    assert 'authorization' not in github.calls('GET', '/blob/9')[0].headers


def test_a_zip_without_run_json_is_not_a_main_run(fetch, github):
    list_runs(github, [run_row(800, SHA_B)])
    serve_artifact(github, 800, 5, {'report.md': 'x'})

    assert fetch().outputs['main_run'] == ''


def test_api_denial_is_a_note_and_never_a_stop(fetch, github, out_dir):
    github.route(
        'GET', rf'/repos/{REPO}/actions/workflows/plurai-support\.yml/runs', {'message': 'Resource not accessible'}, status=403
    )

    result = fetch()

    assert result.code == 0
    assert result.outputs['main_run'] == ''
    assert 'actions: read' in (out_dir / 'notes.md').read_text()


def test_the_token_is_not_logged(fetch, github):
    list_runs(github, [])

    result = fetch()

    assert 'job-token' not in result.stdout + result.stderr


def test_merge_base_comes_from_the_compare_api(fetch, github):
    list_runs(github, [])
    github.route('GET', rf'/repos/{REPO}/compare/{SHA_A}\.\.\.{SHA_B}', {'merge_base_commit': {'sha': SHA_C}})

    result = fetch(base_strategy='merge-base', pr_base=SHA_A, pr_head=SHA_B)

    assert result.outputs['base'] == SHA_C


def test_merge_base_falls_back_to_the_event_base_with_a_warning(fetch, github):
    list_runs(github, [])
    github.route('GET', rf'/repos/{REPO}/compare/{SHA_A}\.\.\.{SHA_B}', {'message': 'boom'}, status=500)

    result = fetch(base_strategy='merge-base', pr_base=SHA_A, pr_head=SHA_B)

    assert result.outputs['base'] == SHA_A
    assert '::warning::' in result.stdout


def test_last_main_run_gives_the_base_for_schedule_and_dispatch(fetch, github):
    list_runs(
        github, [run_row(900, SHA_C), run_row(850, SHA_A, conclusion='cancelled'), run_row(800, SHA_B, conclusion='failure')]
    )

    result = fetch(base_strategy='last-main-run')

    assert result.outputs['base'] == SHA_B


def test_a_dispatch_on_a_branch_diffs_against_its_merge_base_with_the_default_branch(fetch, github):
    list_runs(github, [run_row(800, SHA_A)])
    github.route('GET', rf'/repos/{REPO}/compare/main\.\.\.{SHA_B}', {'merge_base_commit': {'sha': SHA_C}})

    result = fetch(
        base_strategy='last-main-run', GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_REF='refs/heads/feature/x', GITHUB_SHA=SHA_B
    )

    assert result.outputs['base'] == SHA_C


def test_a_dispatch_on_a_branch_still_compares_with_main_s_last_run(fetch, github):
    list_runs(github, [run_row(800, SHA_A)])
    github.route('GET', rf'/repos/{REPO}/compare/main\.\.\.{SHA_B}', {'merge_base_commit': {'sha': SHA_C}})
    serve_artifact(github, 800, 7, {'run.json': '{"ok": true}'})

    result = fetch(
        base_strategy='last-main-run', GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_REF='refs/heads/feature/x', GITHUB_SHA=SHA_B
    )

    assert (result.outputs['main_sha'], result.outputs['base']) == (SHA_A, SHA_C)


def test_a_dispatch_on_the_default_branch_keeps_the_last_main_run_as_its_base(fetch, github):
    list_runs(github, [run_row(800, SHA_C)])

    result = fetch(
        base_strategy='last-main-run', GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_REF='refs/heads/main', GITHUB_SHA=SHA_B
    )

    assert result.outputs['base'] == SHA_C
    assert not [request for request in github.requests if '/compare/' in request.path]


def test_a_schedule_keeps_the_last_main_run_as_its_base(fetch, github):
    list_runs(github, [run_row(800, SHA_C)])

    result = fetch(base_strategy='last-main-run', GITHUB_EVENT_NAME='schedule', GITHUB_REF='refs/heads/main', GITHUB_SHA=SHA_B)

    assert result.outputs['base'] == SHA_C


def test_a_dispatch_on_a_branch_falls_back_to_the_last_main_run_when_the_merge_base_cannot_be_read(fetch, github):
    list_runs(github, [run_row(800, SHA_C)])
    github.route('GET', rf'/repos/{REPO}/compare/main\.\.\.{SHA_B}', {'message': 'boom'}, status=500)

    result = fetch(
        base_strategy='last-main-run', GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_REF='refs/heads/feature/x', GITHUB_SHA=SHA_B
    )

    assert result.outputs['base'] == SHA_C
    assert '::warning::' in result.stdout


def test_event_strategy_keeps_the_sha_from_the_event(fetch, github):
    list_runs(github, [])

    assert fetch(base_strategy='event', base=SHA_A).outputs['base'] == SHA_A


def test_no_strategy_means_no_base(fetch, github):
    list_runs(github, [])

    assert fetch(base_strategy='none').outputs['base'] == ''


def git(*args, cwd):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_the_base_commit_is_fetched_into_a_shallow_checkout(fetch, github, tmp_path):
    origin = tmp_path / 'server' / REPO
    origin.mkdir(parents=True)
    git('init', '-q', '-b', 'main', cwd=origin)
    git('config', 'user.email', 'a@example.invalid', cwd=origin)
    git('config', 'user.name', 'a', cwd=origin)
    (origin / 'f').write_text('1')
    git('add', 'f', cwd=origin)
    git('commit', '-q', '-m', 'one', cwd=origin)
    first = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=origin, capture_output=True, text=True, check=True).stdout.strip()
    (origin / 'f').write_text('2')
    git('commit', '-q', '-am', 'two', cwd=origin)
    git('config', 'uploadpack.allowAnySHA1InWant', 'true', cwd=origin)
    workspace = tmp_path / 'workspace'
    git('clone', '-q', '--depth=1', f'file://{origin}', str(workspace), cwd=tmp_path)
    list_runs(github, [])

    result = fetch(base_strategy='event', base=first, GITHUB_SERVER_URL=f'file://{tmp_path / "server"}')

    assert result.code == 0
    git('cat-file', '-e', f'{first}^{{commit}}', cwd=workspace)


def test_an_unfetchable_base_is_a_warning_and_the_sha_is_still_passed(fetch, github, tmp_path):
    list_runs(github, [])
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    git('init', '-q', cwd=workspace)

    result = fetch(base_strategy='event', base=SHA_A)

    assert result.code == 0
    assert result.outputs['base'] == SHA_A
    assert '::warning::' in result.stdout


def test_default_branch_is_read_from_the_output_of_the_plan(fetch, github):
    list_runs(github, [])

    fetch(SA_DEFAULT_BRANCH='trunk')

    assert 'branch=trunk' in github.calls('GET', rf'/repos/{REPO}/actions/workflows/plurai-support\.yml/runs')[0].query


def test_json_outputs_are_valid(fetch, github):
    list_runs(github, [run_row(800, SHA_B)])
    serve_artifact(github, 800, 5, {'run.json': json.dumps({'a': 1})})

    assert json.loads(Path(fetch().outputs['main_run']).read_text()) == {'a': 1}


def serve_unfiltered(github, rows):
    """A server that ignores the ``event`` filter, so only the script's own checks can reject a row."""
    github.route('GET', RUNS, {'workflow_runs': rows})


def queried_events(github) -> set[str]:
    return {
        pair.split('=', 1)[1] for call in github.calls('GET', RUNS) for pair in call.query.split('&') if pair.startswith('event=')
    }


def test_only_trusted_events_are_queried(fetch, github):
    list_runs(github, [])

    fetch()

    assert queried_events(github) == {'push', 'schedule', 'workflow_dispatch'}


def test_a_fork_pr_whose_branch_is_named_main_cannot_plant_a_main_run(fetch, github):
    planted = run_row(800, SHA_B, event='pull_request', head_repo='mallory/agents')
    serve_unfiltered(github, [planted])
    serve_artifact(github, 800, 5, {'run.json': '{"planted": true}'})

    result = fetch(base_strategy='last-main-run')

    assert result.outputs['main_run'] == ''
    assert result.outputs['main_sha'] == ''
    assert result.outputs['base'] == ''
    assert github.calls('GET', rf'/repos/{REPO}/actions/runs/800/artifacts') == []


def test_a_pull_request_run_of_this_repository_is_not_a_main_run_either(fetch, github):
    serve_unfiltered(github, [run_row(800, SHA_B, event='pull_request')])
    serve_artifact(github, 800, 5, {'run.json': '{}'})

    assert fetch().outputs['main_run'] == ''


@pytest.mark.parametrize('head_repo', ['mallory/agents', None])
def test_a_run_whose_head_repository_is_not_this_one_is_rejected(fetch, github, head_repo):
    serve_unfiltered(github, [run_row(800, SHA_B, event='push', head_repo=head_repo)])
    serve_artifact(github, 800, 5, {'run.json': '{}'})

    result = fetch()

    assert result.outputs['main_run'] == ''
    assert github.calls('GET', rf'/repos/{REPO}/actions/runs/800/artifacts') == []


def test_a_trusted_run_behind_a_planted_one_is_still_found(fetch, github):
    serve_unfiltered(
        github,
        [
            run_row(950, SHA_C, event='pull_request', head_repo='mallory/agents', created='2026-10-31T00:00:00Z'),
            run_row(800, SHA_B, created='2026-10-30T00:00:00Z'),
        ],
    )
    serve_artifact(github, 950, 6, {'run.json': '{"planted": true}'})
    serve_artifact(github, 800, 5, {'run.json': '{"real": true}'})

    result = fetch()

    assert Path(result.outputs['main_run']).read_text() == '{"real": true}'
    assert result.outputs['main_sha'] == SHA_B


def test_scheduled_and_dispatched_runs_count_and_the_newest_wins(fetch, github):
    list_runs(
        github,
        [
            run_row(700, SHA_A, event='push', created='2026-10-29T00:00:00Z'),
            run_row(800, SHA_B, event='schedule', created='2026-10-30T00:00:00Z'),
            run_row(750, SHA_C, event='workflow_dispatch', created='2026-10-29T12:00:00Z'),
        ],
    )
    serve_artifact(github, 800, 5, {'run.json': '{"from": "schedule"}'})

    result = fetch()

    assert Path(result.outputs['main_run']).read_text() == '{"from": "schedule"}'
    assert result.outputs['main_sha'] == SHA_B


def test_git_is_the_binary_the_plan_step_resolved(fetch, github, tmp_path):
    list_runs(github, [])
    log = tmp_path / 'git.log'
    fake = tmp_path / 'fake-git'
    fake.write_text(f'#!/bin/sh\necho "$@" >> {log}\nexit 1\n')
    fake.chmod(0o755)

    result = fetch(base_strategy='event', base=SHA_A, SA_GIT=str(fake))

    assert result.code == 0
    assert f'cat-file -e {SHA_A}' in log.read_text()


def credential_logging_git(tmp_path, log: Path):
    """A stand-in ``git`` that records each call's arguments and its ``GIT_CONFIG_*`` variables; only ``cat-file`` fails."""
    fake = tmp_path / 'fake-git'
    fake.write_text(
        f'#!/bin/sh\nprintf "CALL %s\\n" "$*" >> {log}\n'
        f'env | grep -E "^GIT_CONFIG_(COUNT|KEY|VALUE)" | sed "s/^/CFG /" >> {log}\n'
        'case " $* " in *" cat-file "*) exit 1;; esac\nexit 0\n'
    )
    fake.chmod(0o755)
    return fake


def calls_of(log: Path) -> list[tuple[str, list[str]]]:
    """``(arguments, GIT_CONFIG lines)`` of each git call the stand-in saw, in order."""
    calls: list[tuple[str, list[str]]] = []
    for line in log.read_text().splitlines():
        if line.startswith('CALL '):
            calls.append((line.removeprefix('CALL '), []))
        elif line.startswith('CFG '):
            calls[-1][1].append(line.removeprefix('CFG '))
    return calls


def test_the_credential_reaches_only_the_scratch_repository_fetch(fetch, github, tmp_path):
    list_runs(github, [])
    log = tmp_path / 'git.log'

    result = fetch(
        base_strategy='event',
        base=SHA_A,
        SA_GIT=str(credential_logging_git(tmp_path, log)),
        GITHUB_SERVER_URL='https://github.example',
    )

    calls = calls_of(log)
    holders = [args for args, config in calls if any(line.startswith('GIT_CONFIG_VALUE_0') for line in config)]
    assert len(holders) == 1
    assert ' --git-dir ' in f' {holders[0]} '
    assert holders[0].endswith(f'fetch --no-tags --depth=1 https://github.example/{REPO} {SHA_A}')
    # the workspace's own calls (the cat-file probe and the second hop from the scratch repository) hold no credential
    workspace_calls = [(args, config) for args, config in calls if '--git-dir' not in args and 'init --bare' not in args]
    assert len(workspace_calls) == 2
    assert not [config for _, config in workspace_calls if config and any('VALUE' in line or 'KEY' in line for line in config)]
    credentialed = next(config for args, config in calls if args in holders)
    expected = base64.b64encode(b'x-access-token:job-token').decode()
    assert 'GIT_CONFIG_COUNT=1' in credentialed
    assert 'GIT_CONFIG_KEY_0=http.https://github.example/.extraheader' in credentialed
    assert f'GIT_CONFIG_VALUE_0=AUTHORIZATION: basic {expected}' in credentialed
    assert f'::add-mask::{expected}' in result.stdout
    assert 'job-token' not in result.stdout + result.stderr + log.read_text()


def test_the_credentialed_fetch_runs_outside_the_workspace_in_an_empty_repository(fetch, github, tmp_path):
    """Neither the workspace as working directory nor its git dir: nothing of the checkout's config is read."""
    list_runs(github, [])
    log = tmp_path / 'git.log'
    cwd_log = tmp_path / 'cwd.log'
    fake = tmp_path / 'fake-git'
    fake.write_text(
        f'#!/bin/sh\nprintf "%s|%s\\n" "$PWD" "$*" >> {cwd_log}\ncase " $* " in *" cat-file "*) exit 1;; esac\nexit 0\n'
    )
    fake.chmod(0o755)

    fetch(base_strategy='event', base=SHA_A, SA_GIT=str(fake), GITHUB_SERVER_URL='https://github.example')

    rows = [line.split('|', 1) for line in cwd_log.read_text().splitlines()]
    scratch_hop = next(row for row in rows if 'https://github.example' in row[1])
    assert not Path(scratch_hop[0]).resolve().is_relative_to((tmp_path / 'workspace').resolve())
    assert '--git-dir' in scratch_hop[1]
    assert not log.exists()


def test_a_checkout_config_that_redirects_the_fetch_is_not_read(fetch, github, tmp_path):
    """``url.<x>.insteadOf`` in the checkout would send the fetch (and its credential) elsewhere; the scratch hop ignores it."""
    origin = tmp_path / 'server' / REPO
    origin.mkdir(parents=True)
    git('init', '-q', '-b', 'main', cwd=origin)
    git('config', 'user.email', 'a@example.invalid', cwd=origin)
    git('config', 'user.name', 'a', cwd=origin)
    git('config', 'uploadpack.allowAnySHA1InWant', 'true', cwd=origin)
    first = commit(origin, 'one')
    commit(origin, 'two')
    workspace = tmp_path / 'workspace'
    git('clone', '-q', '--depth=1', f'file://{origin}', str(workspace), cwd=tmp_path)
    server = f'file://{tmp_path / "server"}'
    git('config', f'url.file://{tmp_path / "evil"}/.insteadOf', f'{server}/', cwd=workspace)
    list_runs(github, [])

    result = fetch(base_strategy='event', base=first, GITHUB_SERVER_URL=server, SA_VISIBILITY='private')

    assert result.code == 0
    assert 'could not be fetched' not in result.stdout
    git('cat-file', '-e', f'{first}^{{commit}}', cwd=workspace)


def test_the_scratch_repository_is_removed(fetch, github, tmp_path):
    list_runs(github, [])
    log = tmp_path / 'git.log'

    fetch(base_strategy='event', base=SHA_A, SA_GIT=str(credential_logging_git(tmp_path, log)))

    assert not list(tmp_path.glob('plurai-base-*'))


#: Each way the transport can fail below the HTTP status: the connection closes unanswered, the server stalls past the
#: client's timeout, the body is not JSON.
TRANSPORT_FAILURES = {'disconnect': drop, 'timeout': stall(2), 'bad-json': garbage}


@pytest.fixture(params=sorted(TRANSPORT_FAILURES))
def broken(request):
    """The handler of one transport failure, and the env that makes the client give up on a stall quickly."""
    return TRANSPORT_FAILURES[request.param]


FAST = {'SA_API_TIMEOUT_SECONDS': '0.3'}


def test_a_transport_error_on_the_runs_listing_is_a_note_and_never_a_stop(fetch, github, out_dir, broken):
    github.route('GET', RUNS, broken)

    result = fetch(**FAST)

    assert result.code == 0
    assert result.outputs['main_run'] == ''
    assert 'no main run fetched' in (out_dir / 'notes.md').read_text()
    assert '::warning::' in result.stdout
    assert 'Traceback' not in result.stderr


def test_a_transport_error_on_the_artifact_download_is_a_note_and_never_a_stop(fetch, github, out_dir, broken):
    list_runs(github, [run_row(800, SHA_B)])
    github.route('GET', rf'/repos/{REPO}/actions/runs/800/artifacts', {'artifacts': [{'id': 5, 'name': ARTIFACT_NAME}]})
    github.route('GET', rf'/repos/{REPO}/actions/artifacts/5/zip', broken)

    result = fetch(**FAST)

    assert result.code == 0
    assert result.outputs['main_run'] == ''
    assert 'no main run fetched' in (out_dir / 'notes.md').read_text()


def test_a_transport_error_on_the_blob_redirect_is_a_note_and_never_a_stop(fetch, github, out_dir, broken):
    list_runs(github, [run_row(800, SHA_B)])
    github.route('GET', rf'/repos/{REPO}/actions/runs/800/artifacts', {'artifacts': [{'id': 9, 'name': ARTIFACT_NAME}]})
    github.route(
        'GET', rf'/repos/{REPO}/actions/artifacts/9/zip', lambda _r, _m: (302, b'', {'Location': f'{github.url}/blob/9'})
    )
    github.route('GET', '/blob/9', broken)

    result = fetch(**FAST)

    assert result.code == 0
    assert result.outputs['main_run'] == ''


def test_a_transport_error_on_the_compare_api_falls_back_to_the_event_base(fetch, github, broken):
    list_runs(github, [])
    github.route('GET', rf'/repos/{REPO}/compare/{SHA_A}\.\.\.{SHA_B}', broken)

    result = fetch(base_strategy='merge-base', pr_base=SHA_A, pr_head=SHA_B, **FAST)

    assert result.code == 0
    assert result.outputs['base'] == SHA_A
    assert '::warning::' in result.stdout


def test_a_transport_error_on_a_dispatch_merge_base_falls_back_to_the_last_main_run(fetch, github, broken):
    list_runs(github, [run_row(800, SHA_C)])
    github.route('GET', rf'/repos/{REPO}/compare/main\.\.\.{SHA_B}', broken)

    result = fetch(
        base_strategy='last-main-run',
        GITHUB_EVENT_NAME='workflow_dispatch',
        GITHUB_REF='refs/heads/feature/x',
        GITHUB_SHA=SHA_B,
        **FAST,
    )

    assert result.code == 0
    assert result.outputs['base'] == SHA_C


def test_a_transport_error_on_every_call_still_ends_the_step_green_with_empty_outputs(fetch, github):
    github.route('GET', r'/repos/.*', drop)

    result = fetch(base_strategy='merge-base', pr_base=SHA_A, pr_head=SHA_B, **FAST)

    assert result.code == 0
    assert (result.outputs['main_run'], result.outputs['main_sha']) == ('', '')
    assert result.outputs['base'] == SHA_A


#: Replies that are valid HTTP and valid (or empty) JSON but not the object the client reads: an empty body, ``null``, a list,
#: a string, a number.
UNEXPECTED_REPLIES = {
    'empty-body': b'',
    'null': b'null',
    'list': b'[]',
    'list-of-numbers': b'[1, 2]',
    'string': b'"text"',
    'number': b'7',
}


@pytest.fixture(params=sorted(UNEXPECTED_REPLIES))
def unexpected(request):
    return UNEXPECTED_REPLIES[request.param]


def corrupt_zip() -> bytes:
    """A zip whose directory reads fine and whose compressed member does not inflate (``zlib.error``, not ``BadZipFile``)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('run.json', '{"filler": "' + 'x' * 500 + '"}')
    raw = bytearray(buffer.getvalue())
    start = 30 + len('run.json')  # the local header, then the deflate stream
    raw[start : start + 6] = b'\xff' * 6
    return bytes(raw)


def assert_a_note_and_a_warning_not_a_crash(result, out_dir):
    assert result.code == 0
    assert result.outputs['main_run'] == ''
    assert 'no main run fetched' in (out_dir / 'notes.md').read_text()
    assert '::warning::' in result.stdout
    assert 'Traceback' not in result.stderr


def test_an_unexpected_reply_to_the_runs_listing_is_a_note_and_never_a_stop(fetch, github, out_dir, unexpected):
    github.route('GET', RUNS, lambda _r, _m: (200, unexpected))

    assert_a_note_and_a_warning_not_a_crash(fetch(), out_dir)


def test_an_unexpected_reply_to_the_artifact_listing_is_a_note_and_never_a_stop(fetch, github, out_dir, unexpected):
    list_runs(github, [run_row(800, SHA_B)])
    github.route('GET', rf'/repos/{REPO}/actions/runs/800/artifacts', lambda _r, _m: (200, unexpected))

    assert_a_note_and_a_warning_not_a_crash(fetch(), out_dir)


def test_a_run_row_that_is_not_an_object_is_a_note_and_never_a_stop(fetch, github, out_dir):
    github.route('GET', RUNS, {'workflow_runs': [None, 'text', 3]})

    assert_a_note_and_a_warning_not_a_crash(fetch(), out_dir)


def test_an_artifact_entry_that_is_not_an_object_is_a_note_and_never_a_stop(fetch, github, out_dir):
    list_runs(github, [run_row(800, SHA_B)])
    github.route('GET', rf'/repos/{REPO}/actions/runs/800/artifacts', {'artifacts': [None, 'text']})

    assert_a_note_and_a_warning_not_a_crash(fetch(), out_dir)


def test_a_zip_that_does_not_inflate_is_a_note_and_never_a_stop(fetch, github, out_dir):
    list_runs(github, [run_row(800, SHA_B)])
    github.route('GET', rf'/repos/{REPO}/actions/runs/800/artifacts', {'artifacts': [{'id': 5, 'name': ARTIFACT_NAME}]})
    github.route('GET', rf'/repos/{REPO}/actions/artifacts/5/zip', corrupt_zip())

    assert_a_note_and_a_warning_not_a_crash(fetch(), out_dir)


def test_an_unexpected_reply_to_the_compare_api_falls_back_to_the_event_base(fetch, github, unexpected):
    list_runs(github, [])
    github.route('GET', rf'/repos/{REPO}/compare/{SHA_A}\.\.\.{SHA_B}', lambda _r, _m: (200, unexpected))

    result = fetch(base_strategy='merge-base', pr_base=SHA_A, pr_head=SHA_B)

    assert result.code == 0
    assert result.outputs['base'] == SHA_A
    assert '::warning::' in result.stdout


def test_an_unexpected_reply_to_the_dispatch_merge_base_falls_back_to_the_last_main_run(fetch, github, unexpected):
    list_runs(github, [run_row(800, SHA_C)])
    github.route('GET', rf'/repos/{REPO}/compare/main\.\.\.{SHA_B}', lambda _r, _m: (200, unexpected))

    result = fetch(
        base_strategy='last-main-run', GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_REF='refs/heads/feature/x', GITHUB_SHA=SHA_B
    )

    assert result.code == 0
    assert result.outputs['base'] == SHA_C


def test_an_unexpected_reply_to_the_repository_read_leaves_the_visibility_empty_and_content_out(fetch, github, unexpected):
    list_runs(github, [])
    github.route('GET', rf'/repos/{REPO}', lambda _r, _m: (200, unexpected))

    result = fetch(SA_VISIBILITY='', SA_INCLUDE_CONTENT_SETTING='auto')

    assert result.code == 0
    assert (result.outputs['visibility'], result.outputs['include_content']) == ('', 'false')


def test_a_failure_while_resolving_the_base_is_a_warning_and_leaves_the_base_empty(fetch, github, unexpected):
    """The commit API's reply is read after the checkout cannot be (no git here); a wrong shape must not end the step."""
    list_runs(github, [])
    github.route('GET', rf'/repos/{REPO}/git/commits/{SHA_C}', lambda _r, _m: (200, unexpected))

    result = fetch(base_strategy='merge-base', pr_base=SHA_B, pr_head=SHA_B, GITHUB_SHA=SHA_C, SA_GIT='/nonexistent/git')

    assert result.code == 0
    assert result.outputs['base'] == SHA_B


# --- The suite diff base of a pull request -------------------------------------------------------------------------


def commit(repo: Path, message: str, name: str = 'f') -> str:
    (repo / name).write_text(message)
    git('add', name, cwd=repo)
    git('commit', '-q', '-m', message, cwd=repo)
    return rev(repo, 'HEAD')


def rev(repo: Path, ref: str) -> str:
    return subprocess.run(['git', 'rev-parse', ref], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def pull_request_repo(tmp_path):
    """A server repository where main moved on after the PR's branch point, and the PR's merge commit.

    ``merge`` is ``refs/pull/1/merge``: its parents are main's tip (first) and the PR head (second).
    """
    origin = tmp_path / 'server' / REPO
    origin.mkdir(parents=True)
    git('init', '-q', '-b', 'main', cwd=origin)
    git('config', 'user.email', 'a@example.invalid', cwd=origin)
    git('config', 'user.name', 'a', cwd=origin)
    git('config', 'uploadpack.allowAnySHA1InWant', 'true', cwd=origin)
    git('config', 'commit.gpgsign', 'false', cwd=origin)
    branch_point = commit(origin, 'branch point')
    git('checkout', '-q', '-b', 'pr', cwd=origin)
    head = commit(origin, 'pr change', name='pr-file')
    git('checkout', '-q', 'main', cwd=origin)
    main_tip = commit(origin, 'main moved on', name='main-file')
    git('checkout', '-q', '--detach', main_tip, cwd=origin)
    git('merge', '-q', '--no-ff', '-m', 'merge', head, cwd=origin)
    merge = rev(origin, 'HEAD')
    git('checkout', '-q', 'main', cwd=origin)
    return {'origin': origin, 'branch_point': branch_point, 'head': head, 'main_tip': main_tip, 'merge': merge}


def shallow_checkout(tmp_path, origin: Path, sha: str) -> Path:
    workspace = tmp_path / 'workspace'
    workspace.mkdir(exist_ok=True)
    git('init', '-q', cwd=workspace)
    git('remote', 'add', 'origin', f'file://{origin}', cwd=workspace)
    git('fetch', '-q', '--depth=1', 'origin', sha, cwd=workspace)
    git('checkout', '-q', '--detach', 'FETCH_HEAD', cwd=workspace)
    return workspace


def test_on_a_merge_checkout_the_base_is_the_first_parent_not_the_merge_base(fetch, github, tmp_path, pull_request_repo):
    repo = pull_request_repo
    shallow_checkout(tmp_path, repo['origin'], repo['merge'])
    list_runs(github, [])
    github.route(
        'GET',
        rf'/repos/{REPO}/compare/{repo["main_tip"]}\.\.\.{repo["head"]}',
        {'merge_base_commit': {'sha': repo['branch_point']}},
    )

    result = fetch(
        base_strategy='merge-base',
        pr_base=repo['main_tip'],
        pr_head=repo['head'],
        GITHUB_SHA=repo['merge'],
        GITHUB_EVENT_NAME='pull_request',
    )

    assert result.code == 0
    assert result.outputs['base'] == repo['main_tip']


def test_on_a_merge_checkout_the_base_follows_the_merge_commit_not_the_event_s_base_sha(
    fetch, github, tmp_path, pull_request_repo
):
    """The event's ``base.sha`` can be newer than the base the merge commit was built on; the diff must match the checkout."""
    repo = pull_request_repo
    shallow_checkout(tmp_path, repo['origin'], repo['merge'])
    list_runs(github, [])

    result = fetch(
        base_strategy='merge-base',
        pr_base=SHA_C,
        pr_head=repo['head'],
        GITHUB_SHA=repo['merge'],
        GITHUB_SERVER_URL=f'file://{tmp_path / "server"}',
    )

    assert result.outputs['base'] == repo['main_tip']
    git('cat-file', '-e', f'{repo["main_tip"]}^{{commit}}', cwd=tmp_path / 'workspace')


def test_on_a_pr_head_checkout_the_base_stays_the_merge_base(fetch, github, tmp_path, pull_request_repo):
    repo = pull_request_repo
    shallow_checkout(tmp_path, repo['origin'], repo['head'])
    list_runs(github, [])
    github.route(
        'GET',
        rf'/repos/{REPO}/compare/{repo["main_tip"]}\.\.\.{repo["head"]}',
        {'merge_base_commit': {'sha': repo['branch_point']}},
    )

    result = fetch(base_strategy='merge-base', pr_base=repo['main_tip'], pr_head=repo['head'], GITHUB_SHA=repo['merge'])

    assert result.outputs['base'] == repo['branch_point']


def test_the_run_s_own_sha_decides_the_merge_checkout_not_what_the_workspace_holds_now(
    fetch, github, tmp_path, pull_request_repo
):
    """The caller's install steps ran before this step, so the workspace's HEAD can be anything: the base comes from the
    commit the run was started for."""
    repo = pull_request_repo
    shallow_checkout(tmp_path, repo['origin'], repo['merge'])  # a merge commit whose first parent is main's tip
    list_runs(github, [])
    github.route('GET', rf'/repos/{REPO}/git/commits/{SHA_C}', {'sha': SHA_C, 'parents': [{'sha': SHA_A}, {'sha': SHA_B}]})

    result = fetch(base_strategy='merge-base', pr_base=repo['main_tip'], pr_head=repo['head'], GITHUB_SHA=SHA_C)

    assert result.outputs['base'] == SHA_A


def test_without_a_run_sha_the_workspace_head_is_the_checkout(fetch, github, tmp_path, pull_request_repo):
    repo = pull_request_repo
    shallow_checkout(tmp_path, repo['origin'], repo['merge'])
    list_runs(github, [])

    result = fetch(base_strategy='merge-base', pr_base=repo['main_tip'], pr_head=repo['head'])

    assert result.outputs['base'] == repo['main_tip']


def test_a_checkout_that_cannot_be_read_uses_the_parent_of_the_merge_sha_from_the_api(fetch, github, tmp_path):
    list_runs(github, [])
    github.route('GET', rf'/repos/{REPO}/git/commits/{SHA_C}', {'sha': SHA_C, 'parents': [{'sha': SHA_A}, {'sha': SHA_B}]})

    result = fetch(base_strategy='merge-base', pr_base=SHA_B, pr_head=SHA_B, GITHUB_SHA=SHA_C, SA_GIT='/nonexistent/git')

    assert result.outputs['base'] == SHA_A


# --- The hardening of the token-holding step -----------------------------------------------------------------------

HARDENING = ['core.hooksPath=/dev/null', 'gc.auto=0', 'http.proxy=', 'http.sslVerify=true']


def logging_git(tmp_path, log: Path):
    """A stand-in ``git`` that records its argument vector and the names of its environment; only ``cat-file`` fails."""
    fake = tmp_path / 'fake-git'
    fake.write_text(
        f'#!/bin/sh\nprintf "ARGS %s\\n" "$*" >> {log}\nenv | sed "s/=.*//;s/^/ENV /" >> {log}\n'
        'case " $* " in *" cat-file "*) exit 1;; esac\nexit 0\n'
    )
    fake.chmod(0o755)
    return fake


def test_every_git_call_overrides_what_the_checkout_s_own_config_could_set(fetch, github, tmp_path):
    list_runs(github, [])
    log = tmp_path / 'git.log'

    fetch(base_strategy='event', base=SHA_A, SA_GIT=str(logging_git(tmp_path, log)))

    calls = [line for line in log.read_text().splitlines() if line.startswith('ARGS')]
    assert len(calls) == 5  # the probe, the scratch init and config, the credentialed fetch, the fetch into the workspace
    for call in calls:
        for setting in HARDENING:
            assert f'-c {setting}' in call, (call, setting)
        # the overrides come first, before any subcommand, where git reads them
        assert call.startswith('ARGS -c core.hooksPath=/dev/null ')


def test_git_gets_no_github_token_in_its_environment(fetch, github, tmp_path):
    list_runs(github, [])
    log = tmp_path / 'git.log'

    fetch(base_strategy='event', base=SHA_A, SA_GIT=str(logging_git(tmp_path, log)))

    names = {line.split(maxsplit=1)[1] for line in log.read_text().splitlines() if line.startswith('ENV ')}
    assert 'GITHUB_TOKEN' not in names
    assert 'GH_TOKEN' not in names
    assert 'GITHUB_OUTPUT' not in names
    assert 'GIT_CONFIG_VALUE_0' in names  # the one credential git gets is the scoped extra header


def test_a_checkout_config_cannot_redirect_the_git_calls(tmp_path):
    """Repo-local ``core.hooksPath`` and ``http.proxy`` lose against the ``-c`` overrides the fetch step puts first."""
    import fetch_context

    repo = tmp_path / 'repo'
    repo.mkdir()
    git('init', '-q', cwd=repo)
    git('config', 'core.hooksPath', str(repo / 'hooks'), cwd=repo)
    git('config', 'http.proxy', 'http://127.0.0.1:9', cwd=repo)
    git('config', 'http.sslVerify', 'false', cwd=repo)

    def setting(name: str) -> str:
        done = subprocess.run(
            [*fetch_context.git_command('git'), 'config', '--get', name], cwd=repo, capture_output=True, text=True, check=False
        )
        return done.stdout.strip()

    assert setting('core.hooksPath') == '/dev/null'
    assert setting('http.proxy') == ''
    assert setting('http.sslVerify') == 'true'
    assert setting('gc.auto') == '0'


# --- Which run is "main's" ---------------------------------------------------------------------------------------


def test_a_pull_request_into_a_release_branch_compares_with_that_branch_s_runs(fetch, github):
    list_runs(github, [])

    fetch(SA_MAIN_BRANCH='release/2.x')

    assert 'branch=release%2F2.x' in github.calls('GET', RUNS)[0].query


def test_the_default_branch_is_the_baseline_when_the_plan_names_no_other(fetch, github):
    list_runs(github, [])

    fetch(SA_DEFAULT_BRANCH='trunk', SA_MAIN_BRANCH='')

    assert 'branch=trunk' in github.calls('GET', RUNS)[0].query


# --- A run whose event carries no repository (schedule) -------------------------------------------------------------


def serve_repository(github, visibility='private', **extra):
    github.route('GET', rf'/repos/{REPO}', {'full_name': REPO, 'visibility': visibility, 'default_branch': 'trunk', **extra})


def test_a_run_without_event_visibility_reads_it_from_the_repository(fetch, github):
    list_runs(github, [])
    serve_repository(github, 'private')

    result = fetch(SA_VISIBILITY='', SA_INCLUDE_CONTENT_SETTING='auto')

    assert result.outputs['visibility'] == 'private'
    assert result.outputs['include_content'] == 'true'


def test_a_public_repository_read_from_the_api_keeps_content_out(fetch, github):
    list_runs(github, [])
    serve_repository(github, 'public')

    result = fetch(SA_VISIBILITY='', SA_INCLUDE_CONTENT_SETTING='auto')

    assert (result.outputs['visibility'], result.outputs['include_content']) == ('public', 'false')


def test_an_explicit_include_content_setting_wins_over_the_visibility_read_from_the_api(fetch, github):
    list_runs(github, [])
    serve_repository(github, 'private')

    result = fetch(SA_VISIBILITY='', SA_INCLUDE_CONTENT_SETTING='false')

    assert result.outputs['include_content'] == 'false'


def test_a_visibility_the_plan_already_knows_is_not_read_again(fetch, github):
    list_runs(github, [])

    result = fetch(SA_VISIBILITY='private', SA_INCLUDE_CONTENT_SETTING='auto')

    assert (result.outputs['visibility'], result.outputs['include_content']) == ('private', 'true')
    assert github.calls('GET', rf'/repos/{REPO}') == []


def test_a_repository_that_cannot_be_read_leaves_the_visibility_empty_and_content_out(fetch, github, broken):
    list_runs(github, [])
    github.route('GET', rf'/repos/{REPO}', broken)

    result = fetch(SA_VISIBILITY='', SA_INCLUDE_CONTENT_SETTING='auto', **FAST)

    assert result.code == 0
    assert (result.outputs['visibility'], result.outputs['include_content']) == ('', 'false')


# --- Nothing but a commit id reaches git; git sees none of the job's steering variables ----------------------------------

INJECTION = '--upload-pack=touch /tmp/pwned'


@pytest.mark.parametrize('bad', [INJECTION, '-c', 'main', 'A' * 40, SHA_A[:39]])
def test_a_base_that_is_not_a_commit_id_never_reaches_git(fetch, github, tmp_path, bad):
    list_runs(github, [])
    log = tmp_path / 'git.log'

    result = fetch(base_strategy='event', base=bad, SA_GIT=str(logging_git(tmp_path, log)))

    assert result.code == 0
    assert result.outputs['base'] == ''
    assert '::warning::' in result.stdout
    assert not log.exists() or bad not in log.read_text()


def test_a_parent_that_is_not_a_commit_id_is_dropped(fetch, github, tmp_path):
    list_runs(github, [])
    github.route('GET', rf'/repos/{REPO}/git/commits/{SHA_C}', {'parents': [{'sha': INJECTION}, {'sha': SHA_B}]})

    result = fetch(base_strategy='merge-base', pr_base=SHA_B, pr_head=SHA_B, GITHUB_SHA=SHA_C, SA_GIT='/nonexistent/git')

    assert result.outputs['base'] == SHA_B  # one valid parent is not a merge: the merge-base path ran
    assert INJECTION not in result.stdout


def test_a_main_run_whose_head_sha_is_not_a_commit_id_is_not_used(fetch, github):
    list_runs(github, [run_row(800, INJECTION)])
    serve_artifact(github, 800, 5, {'run.json': '{}'})

    result = fetch()

    assert result.outputs['main_run'] == ''


def test_the_base_is_fetched_from_this_repository_on_the_job_server_not_from_origin(fetch, github, tmp_path):
    list_runs(github, [])
    log = tmp_path / 'git.log'

    fetch(base_strategy='event', base=SHA_A, SA_GIT=str(logging_git(tmp_path, log)), GITHUB_SERVER_URL='https://github.example')

    calls = [line for line in log.read_text().splitlines() if line.startswith('ARGS')]
    assert len([c for c in calls if c.endswith(f'https://github.example/{REPO} {SHA_A}')]) == 1
    assert not [c for c in calls if ' origin ' in c]


def test_the_ext_transport_is_refused_whatever_the_checkout_config_says(tmp_path):
    import fetch_context

    repo = tmp_path / 'repo'
    repo.mkdir()
    git('init', '-q', cwd=repo)
    git('config', 'protocol.ext.allow', 'always', cwd=repo)

    done = subprocess.run(
        [*fetch_context.git_command('git'), 'config', '--get', 'protocol.ext.allow'],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.stdout.strip() == 'never'


def test_git_gets_none_of_the_variables_that_steer_it(fetch, github, tmp_path):
    list_runs(github, [])
    log = tmp_path / 'git.log'

    fetch(
        base_strategy='event',
        base=SHA_A,
        SA_GIT=str(logging_git(tmp_path, log)),
        GIT_SSH_COMMAND='touch /tmp/pwned',
        GIT_EXEC_PATH='/nonexistent/evil',
        HTTPS_PROXY='http://127.0.0.1:9',
        https_proxy='http://127.0.0.1:9',
        CURL_CA_BUNDLE='/nonexistent/ca',
        GIT_CONFIG_PARAMETERS="'core.pager=touch /tmp/pwned'",
    )

    names = {line.split(maxsplit=1)[1] for line in log.read_text().splitlines() if line.startswith('ENV ')}
    assert not names & {
        'GIT_SSH_COMMAND',
        'GIT_EXEC_PATH',
        'HTTPS_PROXY',
        'https_proxy',
        'CURL_CA_BUNDLE',
        'GIT_CONFIG_PARAMETERS',
    }
    assert {'GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL', 'GIT_NO_REPLACE_OBJECTS'} <= names
