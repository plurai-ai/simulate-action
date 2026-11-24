"""Main's latest completed ``run.json``, the base commit of the suite diff and the repository's visibility.

Holds the job's read token, so it runs in a step of its own: the token never reaches ``plurai ci run``. It runs after the
caller's own install steps, so the project's code has already run on this runner: git is called with the checkout's own
config overridden on the command line and without the token in its environment, and the interpreter is run with ``-S -E``
by the action. Every failure here is a warning or a note and never a stop: with no main run the "vs main" column is
skipped, with no base the suite diff degrades to a note in the SDK, and with no visibility the content stays out.
"""

from __future__ import annotations

import base64
import io
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

import common
from github import MAX_DOWNLOAD_BYTES, ApiError, GitHubApi

#: How many of main's completed runs are looked through for one with a usable artifact.
RUNS_SCANNED = 10
#: A run that ended by a verdict (green or red). Cancelled and skipped runs measured nothing to compare with.
COMPARABLE = ('success', 'failure')
#: A run whose commit is a fair "last commit that ran", for a scheduled or dispatched run's suite diff base.
RAN = ('success', 'failure', 'timed_out')
#: Seconds a git call may take; a hung fetch must not hold the job.
GIT_TIMEOUT_SECONDS = 120
#: Settings put before every git subcommand. They win over the checkout's own ``.git/config``, which the project's install
#: steps could have written: no hooks, no background gc, no proxy, TLS verified.
GIT_OVERRIDES = (
    'core.hooksPath=/dev/null',
    'gc.auto=0',
    'http.proxy=',
    'http.sslVerify=true',
    'protocol.ext.allow=never',
)
#: A commit id as git prints it (SHA-1 or SHA-256). Nothing else reaches a git command line: a value that starts with ``-``
#: would be read as an option.
COMMIT_ID = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?')
#: Variables of the inherited environment that steer git or its transport; none survives into a git call.
GIT_STEERING = re.compile(r'^(?:GIT_.*|.*_proxy|CURL_.*|SSL_CERT_(?:FILE|DIR))$', re.IGNORECASE)


def is_commit_id(value: object) -> bool:
    return isinstance(value, str) and COMMIT_ID.fullmatch(value) is not None


def artifact_name(key: str) -> str:
    return f'plurai-{key}-report'


def workflow_file(env: Mapping[str, str]) -> str:
    ref = env.get('GITHUB_WORKFLOW_REF', '')
    marker = '/.github/workflows/'
    if marker not in ref:
        return ''
    return ref.split(marker, 1)[1].split('@', 1)[0]


#: The events whose runs on the default branch are the repository's own. A ``pull_request`` run can carry the default
#: branch's name as its head branch (a fork PR from its own ``main``), so it is never a source of main's record.
TRUSTED_EVENTS = ('push', 'schedule', 'workflow_dispatch')


def is_trusted_run(row: Mapping[str, Any], repository: str) -> bool:
    """A run that executed this repository's own code on a trusted event, not a fork's or a PR's."""
    head_repository = row.get('head_repository') or {}
    return row.get('event') in TRUSTED_EVENTS and head_repository.get('full_name') == repository


def main_runs(api: GitHubApi, env: Mapping[str, str]) -> list[dict[str, Any]]:
    """Completed runs of this workflow on the baseline branch (the pull request's base, else the default), newest first,
    without the current run.

    One query per trusted event, then each row is checked again: the server-side filter narrows the list, the check is
    what keeps a planted artifact out.
    """
    file = workflow_file(env)
    if not file:
        return []
    repository = env['GITHUB_REPOSITORY']
    branch = quote(env.get('SA_MAIN_BRANCH') or env.get('SA_DEFAULT_BRANCH') or 'main', safe='')
    current = env.get('GITHUB_RUN_ID', '')
    rows: list[dict[str, Any]] = []
    for event in TRUSTED_EVENTS:
        path = f'/repos/{repository}/actions/workflows/{file}/runs?branch={branch}&event={event}&status=completed'
        rows += api.get(f'{path}&per_page={RUNS_SCANNED + 1}')['workflow_runs']
    trusted = [row for row in rows if is_trusted_run(row, repository) and str(row['id']) != current]
    return sorted(trusted, key=lambda row: row.get('created_at') or '', reverse=True)


def download_run_json(api: GitHubApi, repository: str, run_id: int, name: str, destination: Path) -> bool:
    listing = api.get(f'/repos/{repository}/actions/runs/{run_id}/artifacts?name={name}')
    for artifact in listing.get('artifacts', []):
        if artifact.get('name') != name or artifact.get('expired'):
            continue
        blob = api.download(f'/repos/{repository}/actions/artifacts/{artifact["id"]}/zip')
        if len(blob) > MAX_DOWNLOAD_BYTES:
            continue
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            if 'run.json' not in archive.namelist():
                continue
            with archive.open('run.json') as member:
                data = member.read(MAX_DOWNLOAD_BYTES + 1)
        if len(data) > MAX_DOWNLOAD_BYTES:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return True
    return False


def find_main_run(api: GitHubApi, env: Mapping[str, str], runs: list[dict[str, Any]]) -> tuple[str, str]:
    repository = env['GITHUB_REPOSITORY']
    name = artifact_name(env.get('SA_WORKFLOW_KEY') or common.workflow_key(env))
    destination = Path(env.get('RUNNER_TEMP') or '.') / 'plurai-main' / 'run.json'
    for row in [r for r in runs if r.get('conclusion') in COMPARABLE and is_commit_id(r.get('head_sha'))][:RUNS_SCANNED]:
        if download_run_json(api, repository, row['id'], name, destination):
            return str(destination), row['head_sha']
    return '', ''


def merge_base(api: GitHubApi, repository: str, base: str, head: str) -> str:
    try:
        return api.get(f'/repos/{repository}/compare/{base}...{head}')['merge_base_commit']['sha']
    except (ApiError, KeyError, TypeError) as error:
        common.warning(f'the PR merge-base could not be read ({error}); the suite diff uses the PR base commit instead')
        return base


def dispatch_merge_base(api: GitHubApi, env: Mapping[str, str]) -> str:
    """The merge-base of a manual dispatch's commit with the default branch, or ``''`` when the run is not a dispatch off the
    default branch or the merge-base cannot be read.

    A branch's dispatch is a pull request's suite diff in all but the event: main's last run would list every unrelated change
    that landed on main since, so the base is where the branch left main.
    """
    if env.get('GITHUB_EVENT_NAME') != 'workflow_dispatch':
        return ''
    default = env.get('SA_DEFAULT_BRANCH') or 'main'
    head = env.get('GITHUB_SHA', '')
    if not head or env.get('GITHUB_REF', '') == f'refs/heads/{default}':
        return ''
    try:
        return api.get(f'/repos/{env["GITHUB_REPOSITORY"]}/compare/{quote(default, safe="/")}...{head}')['merge_base_commit'][
            'sha'
        ]
    except (ApiError, KeyError, TypeError) as error:
        common.warning(
            f'the merge-base with {default} could not be read ({error}); the suite diff uses the last run on {default} instead'
        )
        return ''


def git_command(git: str) -> list[str]:
    """The git binary with the hardening overrides in front, where git reads them."""
    return [git, *(arg for setting in GIT_OVERRIDES for arg in ('-c', setting))]


def git_env(env: Mapping[str, str]) -> dict[str, str]:
    """The environment of a git call: the job's, without the names that carry a GitHub credential or a step file and
    without the ones that steer git or its transport (``GIT_*``, proxies, ``CURL_*``), and with no system or global config."""
    kept = {name: value for name, value in env.items() if name not in common.STRIPPED and not GIT_STEERING.match(name)}
    return {
        **kept,
        'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_NO_REPLACE_OBJECTS': '1',
        'GIT_TERMINAL_PROMPT': '0',
    }


def _git(
    env: Mapping[str, str], *args: str, extra: Mapping[str, str] | None = None, cwd: str | os.PathLike[str] | None = None
) -> subprocess.CompletedProcess[str] | None:
    """One hardened git call, in the workspace unless ``cwd`` says otherwise; None when git cannot be run or takes too long."""
    git = env.get('SA_GIT') or 'git'
    try:
        return subprocess.run(
            [*git_command(git), *args],
            cwd=cwd or env.get('GITHUB_WORKSPACE') or '.',
            env={**git_env(os.environ), **(extra or {})},
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def checkout_head(env: Mapping[str, str]) -> str:
    """The commit the workspace has checked out; empty when git cannot say."""
    done = _git(env, 'rev-parse', 'HEAD')
    head = done.stdout.strip() if done and done.returncode == 0 else ''
    return head if is_commit_id(head) else ''


def checked_out_commit(env: Mapping[str, str], pr_head: str) -> str:
    """The commit the code under test is: the run's own ``GITHUB_SHA`` (the merge commit of a pull request), or the PR head
    when the workflow checked that out instead.

    The caller's install steps ran before this step, so the workspace's HEAD is the project's to move. It is read as a yes or
    no question (is it the PR head the event named?) and never as a source of the base; the workspace's HEAD stands in for
    ``GITHUB_SHA`` only when that is unset.
    """
    run_sha = env.get('GITHUB_SHA', '')
    if not run_sha:
        return checkout_head(env)
    return pr_head if pr_head and checkout_head(env) == pr_head else run_sha


def parents_of(api: GitHubApi, env: Mapping[str, str], sha: str) -> list[str]:
    """The parents of ``sha``, from the checkout (the commit object lists them even in a shallow clone), else the API."""
    if not is_commit_id(sha):
        return []
    done = _git(env, 'cat-file', 'commit', sha)
    if done and done.returncode == 0:
        header = done.stdout.split('\n\n', 1)[0]
        found = [line.split()[1] for line in header.splitlines() if line.startswith('parent ') and len(line.split()) == 2]
    else:
        try:
            reply = api.get(f'/repos/{env["GITHUB_REPOSITORY"]}/git/commits/{sha}')
            found = [parent['sha'] for parent in reply['parents']]
        except (ApiError, KeyError, TypeError):
            return []
    return [parent for parent in found if is_commit_id(parent)]


def pull_request_base(api: GitHubApi, env: Mapping[str, str]) -> str:
    """The base of a pull request's suite diff, matched to the commit under test.

    On the default merge checkout (``refs/pull/N/merge``) the code under test is the merge commit, whose first parent is
    the base branch as the merge saw it: the diff against it is the PR's own change, even when main moved after the branch
    point. On a checkout of the PR head the merge-base of the base and the head is the same thing.
    """
    base, head = env.get('SA_PR_BASE_SHA', ''), env.get('SA_PR_HEAD_SHA', '')
    checkout = checked_out_commit(env, head)
    if checkout and head and checkout != head:
        parents = parents_of(api, env, checkout)
        if len(parents) >= 2:
            return parents[0]
    return merge_base(api, env['GITHUB_REPOSITORY'], base, head) if base and head else base


def resolve_base(api: GitHubApi, env: Mapping[str, str], runs: list[dict[str, Any]]) -> str:
    strategy = env.get('SA_BASE_STRATEGY', 'none')
    if strategy == 'event':
        return env.get('SA_BASE', '')
    if strategy == 'merge-base':
        return pull_request_base(api, env)
    if strategy == 'last-main-run':
        return dispatch_merge_base(api, env) or next((row['head_sha'] for row in runs if row.get('conclusion') in RAN), '')
    return ''


def _auth_env(env: Mapping[str, str]) -> dict[str, str]:
    """Git config for one call, through the environment: the checkout keeps no credential (`persist-credentials: false`)."""
    token = env.get('GITHUB_TOKEN', '')
    if not token:
        return {}
    server = (env.get('GITHUB_SERVER_URL') or 'https://github.com').rstrip('/')
    encoded = base64.b64encode(f'x-access-token:{token}'.encode()).decode()
    print(f'::add-mask::{encoded}')
    return {
        'GIT_CONFIG_COUNT': '1',
        'GIT_CONFIG_KEY_0': f'http.{server}/.extraheader',
        'GIT_CONFIG_VALUE_0': f'AUTHORIZATION: basic {encoded}',
    }


def repository_url(env: Mapping[str, str]) -> str:
    """Where the base commit is fetched from: this repository on the job's server, never the checkout's ``origin`` (its
    URL and ``remote.origin.*`` settings are the checkout's own config, which project code could have rewritten)."""
    server = (env.get('GITHUB_SERVER_URL') or 'https://github.com').rstrip('/')
    return f'{server}/{env["GITHUB_REPOSITORY"]}'


def fetch_commit(env: Mapping[str, str], sha: str) -> None:
    """Make the base commit readable in the shallow checkout; a failure is a warning, the sha is still passed."""
    if not is_commit_id(sha):
        common.warning('the base commit is not a commit id; the suite diff may be unavailable')
        return
    present = _git(env, 'cat-file', '-e', f'{sha}^{{commit}}')
    if present and present.returncode == 0:
        return
    if not fetch_through_scratch_repository(env, sha):
        common.warning(f'the base commit {sha[:12]} could not be fetched; the suite diff may be unavailable')


def fetch_through_scratch_repository(env: Mapping[str, str], sha: str) -> bool:
    """Fetch ``sha`` into the workspace in two hops, so the credential never meets the checkout's config.

    The checkout's ``.git/config`` is the project's to write (its install steps ran before this step), and it can redirect a
    fetch in ways the command line cannot cancel: ``url.<x>.insteadOf``, a URL-specific ``http.<server>/.proxy`` or
    ``sslVerify``. The first hop therefore runs in an empty bare repository that holds no config of the project's and is the
    only call with the credential. The second hop fetches the commit from that scratch repository into the workspace with no
    credential at all.
    """
    parent = env.get('RUNNER_TEMP') or None
    try:
        with tempfile.TemporaryDirectory(prefix='plurai-base-', dir=parent) as scratch:
            bare = str(Path(scratch) / 'repo.git')
            steps = (
                (('init', '--bare', '-q', bare), {}, scratch),
                (('--git-dir', bare, 'config', 'uploadpack.allowAnySHA1InWant', 'true'), {}, scratch),
                (('--git-dir', bare, 'fetch', '--no-tags', '--depth=1', repository_url(env), sha), _auth_env(env), scratch),
                (('fetch', '--no-tags', '--depth=1', bare, sha), {}, None),
            )
            for args, extra, cwd in steps:
                done = _git(env, *args, extra=extra, cwd=cwd)
                if done is None or done.returncode != 0:
                    return False
    except OSError:
        return False
    return True


def repository_visibility(api: GitHubApi, env: Mapping[str, str], directory: Path) -> str:
    """The repository's visibility: the plan's, else read from the API (an event such as ``schedule`` carries no repository);
    empty when it cannot be read, which keeps transcripts and judge text off GitHub."""
    known = env.get('SA_VISIBILITY', '')
    if known:
        return known
    try:
        data = api.get(f'/repos/{env["GITHUB_REPOSITORY"]}')
        visibility = str(data.get('visibility') or ('private' if data.get('private') else 'public'))
    except (ApiError, AttributeError, KeyError, TypeError) as error:
        common.add_note(directory, f'the repository visibility could not be read ({error}); content stays off GitHub')
        common.warning(f'the repository visibility could not be read: {error}')
        return ''
    return visibility if visibility in common.VISIBILITIES else ''


def _describe(error: Exception) -> str:
    """One line for a failure: the message, with the exception's kind when the message alone says little."""
    return str(error) if isinstance(error, ApiError) else f'{type(error).__name__}: {error}'.rstrip(': ')


def main(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    directory = common.out_dir(env)
    api = GitHubApi.from_env(env)
    runs: list[dict[str, Any]] = []
    main_run, main_sha = '', ''
    try:
        runs = main_runs(api, env)
        main_run, main_sha = find_main_run(api, env, runs)
    except Exception as error:
        # Any failure ends as a note and a warning: this step has no reason to stop the job. The kinds are open-ended (a reply
        # that is valid JSON of the wrong shape, an archive that does not inflate), so the handler is not a list of them.
        common.add_note(
            directory, f'no main run fetched ({_describe(error)}); the workflow needs `actions: read` to compare with main'
        )
        common.warning(f"main's run.json could not be fetched: {_describe(error)}")
    try:
        base = resolve_base(api, env, runs)
    except Exception as error:  # the suite diff degrades to a note in the SDK when there is no base
        common.warning(f'the suite diff base could not be resolved ({_describe(error)}); it is dropped')
        base = ''
    if base and not is_commit_id(base):
        common.warning('the suite diff base is not a commit id and is dropped')
        base = ''
    if base:
        fetch_commit(env, base)
    visibility = repository_visibility(api, env, directory)
    setting = env.get('SA_INCLUDE_CONTENT_SETTING') or 'auto'
    common.set_output('main_run', main_run, env)
    common.set_output('main_sha', main_sha, env)
    common.set_output('base', base, env)
    common.set_output('visibility', visibility, env)
    common.set_output('include_content', 'true' if common.resolve_include_content(setting, visibility) else 'false', env)
    return common.OK


if __name__ == '__main__':
    sys.exit(main())
