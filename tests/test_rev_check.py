"""rev_check.py: warns when the workflow's managed block is older than this release expects."""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def check(run_script, tmp_path, out_dir):
    workspace = tmp_path / 'workspace'
    (workspace / '.github' / 'workflows').mkdir(parents=True)
    release = tmp_path / 'release.json'
    release.write_text(json.dumps({'managed_rev': 3}))

    def run(workflow_text: str | None, ref: str = 'acme/agents/.github/workflows/plurai-support.yml@refs/heads/main'):
        if workflow_text is not None:
            (workspace / '.github' / 'workflows' / 'plurai-support.yml').write_text(workflow_text)
        env = {'GITHUB_WORKSPACE': str(workspace), 'GITHUB_WORKFLOW_REF': ref, 'SA_RELEASE_FILE': str(release)}
        return run_script('rev_check.py', env)

    return run


def test_an_older_rev_warns_and_leaves_a_note(check, out_dir):
    result = check('# plurai:managed rev=2 — re-render with /simulation-ci\nname: x\n')

    assert result.code == 0
    assert '::warning::' in result.stdout
    note = (out_dir / 'notes.md').read_text()
    assert 'rev=2' in note
    assert 'rev=3' in note
    assert '/simulation-ci' in note


@pytest.mark.parametrize('rev', [3, 4])
def test_a_current_or_newer_rev_is_silent(check, out_dir, rev):
    result = check(f'# plurai:managed rev={rev}\nname: x\n')

    assert '::warning::' not in result.stdout
    assert not (out_dir / 'notes.md').exists()


def test_a_workflow_without_the_marker_is_silent(check, out_dir):
    result = check('name: hand written\n')

    assert result.code == 0
    assert not (out_dir / 'notes.md').exists()


def test_the_marker_counts_only_near_the_top(check, out_dir):
    check('\n'.join(['# a'] * 10) + '\n# plurai:managed rev=1\n')

    assert not (out_dir / 'notes.md').exists()


def test_an_unreadable_workflow_never_fails_the_step(check):
    assert check(None).code == 0


def test_a_missing_workflow_ref_never_fails_the_step(check):
    assert check('# plurai:managed rev=1\n', ref='').code == 0
