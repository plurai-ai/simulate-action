"""assert_run.py: the fixture workflow's assertions fail on a wrong run and pass on a right one."""

from __future__ import annotations

import json

import pytest
from assert_run import main

BASE = ['--event', 'pull_request', '--repo', 'acme/agents', '--url', 'https://stub.invalid']


@pytest.fixture
def run_dirs(tmp_path):
    out, report, transcripts = tmp_path / 'out', tmp_path / 'report', tmp_path / 'transcripts'
    for directory in (out, report, transcripts):
        directory.mkdir()
    (out / 'job-summary.md').write_text('## Plurai simulation · ❌ 2 failed\n')
    (out / 'stub-record.json').write_text(
        json.dumps(
            {
                'argv': ['ci', 'run', '--repo', 'acme/agents', '--selection', 'smoke', '--base', 'a' * 40],
                'exit': 1,
                'env': {'PLURAI_URL': 'https://stub.invalid', 'PLURAI_TOKEN': '<set>'},
            }
        )
    )
    for name in ('run.json', 'report.md', 'junit.xml'):
        (report / name).write_text('x')
    (transcripts / 't.json').write_text('{}')

    def args(**overrides):
        values = {
            '--out-dir': out,
            '--outcome': 'failure',
            '--expect-exit': 1,
            '--heading': '❌ 2 failed',
            '--report-dir': report,
            '--transcripts-dir': transcripts,
            **overrides,
        }
        return BASE + [str(item) for pair in values.items() for item in pair]

    return args, out


def test_a_right_run_passes(run_dirs):
    args, _ = run_dirs

    assert main(args()) == 0


def test_an_empty_expected_url_requires_plurai_url_to_be_absent(run_dirs):
    args, out = run_dirs
    record = json.loads((out / 'stub-record.json').read_text())

    assert main(args(**{'--url': ''})) == 1

    del record['env']['PLURAI_URL']
    (out / 'stub-record.json').write_text(json.dumps(record))

    assert main(args(**{'--url': ''})) == 0
    assert main(args()) == 1


def test_a_github_token_in_the_stub_environment_fails(run_dirs):
    args, out = run_dirs
    record = json.loads((out / 'stub-record.json').read_text())
    record['env']['GITHUB_TOKEN'] = '<set>'
    (out / 'stub-record.json').write_text(json.dumps(record))

    assert main(args()) == 1


def test_a_wrong_selection_fails(run_dirs):
    args, _ = run_dirs

    assert main(args(**{'--expect-selection': 'full'})) == 1


def test_a_success_outcome_on_a_failing_code_fails(run_dirs):
    args, _ = run_dirs

    assert main(args(**{'--outcome': 'success'})) == 1


def test_a_wrong_heading_fails(run_dirs):
    args, _ = run_dirs

    assert main(args(**{'--heading': '✅'})) == 1


def test_transcripts_present_when_excluded_fails(run_dirs):
    args, _ = run_dirs

    assert main(args(**{'--expect-transcripts': 'absent'})) == 1
    assert main(args(**{'--expect-transcripts': 'present'})) == 0


def test_a_missing_report_file_fails(run_dirs, tmp_path):
    args, _ = run_dirs
    (tmp_path / 'report' / 'junit.xml').unlink()

    assert main(args()) == 1


def test_the_project_venv_must_lead_the_path(run_dirs):
    args, out = run_dirs
    record = json.loads((out / 'stub-record.json').read_text())
    record['env']['PATH'] = '/work/.venv/bin:/usr/bin'
    (out / 'stub-record.json').write_text(json.dumps(record))

    assert main(args(**{'--expect-bin-dir': '/work/.venv/bin'})) == 0
    assert main(args(**{'--expect-bin-dir': '/other/.venv/bin'})) == 1


def set_action_version(out, value):
    record = json.loads((out / 'stub-record.json').read_text())
    record['env']['PLURAI_ACTION_VERSION'] = value
    (out / 'stub-record.json').write_text(json.dumps(record))


def test_a_local_run_expects_no_action_version(run_dirs):
    args, out = run_dirs
    set_action_version(out, 'v1')

    assert main(args()) == 1


def test_an_expected_action_version_must_match(run_dirs):
    args, out = run_dirs
    set_action_version(out, 'v1')

    assert main(args(**{'--expect-action-version': 'v1'})) == 0
    assert main(args(**{'--expect-action-version': 'v2'})) == 1
