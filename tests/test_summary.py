"""summary.py: composes the job summary and the final PR comment from what the run left in the out dir."""

from __future__ import annotations

import pytest

MARKER = '<!-- plurai-simulation-report -->'
RESULTS = '## Plurai simulation · ❌ 2 failed · 0 unstable · 0 advisory · 0 not measured\n\n### Failed\n- a / b\n'
DIFF = '### Suite changed vs base\n\n- evals 91c2… → a7d0…\n'


@pytest.fixture
def compose(run_script, out_dir, tmp_path):
    def run(mode: str = 'summary', **env):
        result = run_script('summary.py', {'SA_RUN_OUTCOME': 'failure', **env}, mode)
        summary = tmp_path / 'github_step_summary'
        return result, (summary.read_text() if summary.exists() else '')

    return run


def test_results_come_first_then_the_suite_diff(compose, out_dir):
    (out_dir / 'summary.md').write_text(RESULTS)
    (out_dir / 'suite-diff.md').write_text(DIFF)

    result, appended = compose()

    assert result.code == 0
    assert appended.startswith('## Plurai simulation · ❌ 2 failed')
    assert appended.index('### Failed') < appended.index('### Suite changed vs base')
    assert (out_dir / 'job-summary.md').read_text() == appended


def test_no_suite_diff_means_none_in_the_summary(compose, out_dir):
    (out_dir / 'summary.md').write_text(RESULTS)

    _, appended = compose()

    assert 'Suite changed' not in appended


def test_notes_of_the_action_close_the_summary(compose, out_dir):
    (out_dir / 'summary.md').write_text(RESULTS)
    (out_dir / 'suite-diff.md').write_text(DIFF)
    (out_dir / 'notes.md').write_text('- PR comment skipped: the PR comes from a fork.\n')

    _, appended = compose()

    assert appended.rstrip().endswith('- PR comment skipped: the PR comes from a fork.')
    assert appended.index('Suite changed vs base') < appended.index('Notes from the action')


def test_a_run_that_wrote_no_summary_gets_a_heading_naming_the_outcome(compose):
    _, appended = compose(SA_RUN_OUTCOME='cancelled')

    first = appended.splitlines()[0]
    assert first.startswith('## Plurai simulation · ⛔ not measured')
    assert 'cancelled' in first


def test_a_failed_run_without_a_summary_points_to_the_log(compose):
    _, appended = compose(SA_RUN_OUTCOME='failure')

    assert appended.splitlines()[0].startswith('## Plurai simulation · ⛔')
    assert 'job log' in appended


def test_an_oversized_diff_loses_its_details_first(compose, out_dir):
    (out_dir / 'summary.md').write_text(RESULTS)
    big = DIFF + '<details><summary>body</summary>\n' + 'x' * 1_100_000 + '\n</details>\n'
    (out_dir / 'suite-diff.md').write_text(big)

    _, appended = compose()

    assert len(appended.encode()) <= 1024 * 1024
    assert '### Suite changed vs base' in appended
    assert 'xxxx' not in appended
    assert 'suite-diff.md' in appended


def test_a_diff_that_stays_too_big_without_details_becomes_a_pointer(compose, out_dir):
    (out_dir / 'summary.md').write_text(RESULTS)
    (out_dir / 'suite-diff.md').write_text(DIFF + '- row\n' * 400_000)

    _, appended = compose()

    assert len(appended.encode()) <= 1024 * 1024
    assert 'suite-diff.md' in appended
    assert '- row' not in appended


def test_the_final_comment_is_the_results_comment_then_the_short_diff(compose, out_dir):
    (out_dir / 'comment.md').write_text(f'{MARKER}\n{RESULTS}')
    (out_dir / 'suite-diff-comment.md').write_text(DIFF)

    compose('comment')

    body = (out_dir / 'comment-final.md').read_text()
    assert body.startswith(f'{MARKER}\n## Plurai simulation · ❌')
    assert body.index('### Failed') < body.index('### Suite changed vs base')


def test_the_final_comment_drops_the_diff_when_it_would_pass_the_limit(compose, out_dir):
    (out_dir / 'comment.md').write_text(f'{MARKER}\n' + 'r' * 60_000)
    (out_dir / 'suite-diff-comment.md').write_text('d' * 10_000)

    compose('comment')

    body = (out_dir / 'comment-final.md').read_text()
    assert len(body) <= 65_536
    assert 'ddd' not in body
    assert 'suite-diff.md' in body


def test_a_run_without_a_comment_body_falls_back_to_the_summary_heading(compose, out_dir):
    compose('comment', SA_RUN_OUTCOME='failure')

    assert (out_dir / 'comment-final.md').read_text().startswith(f'{MARKER}\n## Plurai simulation · ⛔')


def test_there_is_no_pre_run_comment_mode(compose, out_dir):
    (out_dir / 'suite-diff-comment.md').write_text(DIFF)

    result, _ = compose('pending')

    assert result.code == 65
    assert not (out_dir / 'comment-pending.md').exists()


def test_the_final_comment_carries_the_suite_diff_after_the_results(compose, out_dir):
    (out_dir / 'comment.md').write_text(f'{MARKER}\n{RESULTS}')
    (out_dir / 'suite-diff-comment.md').write_text(DIFF)

    compose('comment')

    body = (out_dir / 'comment-final.md').read_text()
    assert body.index('### Failed') < body.index('### Suite changed vs base')


def first_line(text: str) -> str:
    return text.splitlines()[0]


def test_a_run_step_that_never_started_is_not_blamed_on_plurai_ci_run(compose):
    _, appended = compose(SA_RUN_OUTCOME='skipped')

    assert first_line(appended) == '## Plurai simulation · ⛔ not measured: the run did not start'
    assert 'ended without writing' not in appended
    assert 'before `plurai ci run` started' in appended


@pytest.mark.parametrize(
    ('step', 'label'),
    [
        ('SA_INSTALL_OUTCOME', 'the SDK install'),
        ('SA_FETCH_OUTCOME', "the fetch of main's run"),
        ('SA_PLAN_OUTCOME', 'the plan'),
    ],
)
def test_the_step_that_failed_before_the_run_is_named(compose, step, label):
    _, appended = compose(SA_RUN_OUTCOME='skipped', **{step: 'failure'})

    assert label in appended


def test_the_first_failed_step_is_the_one_named(compose):
    _, appended = compose(SA_RUN_OUTCOME='skipped', SA_FETCH_OUTCOME='failure', SA_INSTALL_OUTCOME='failure')

    assert "the fetch of main's run" in appended
    assert 'the SDK install' not in appended


def test_a_run_that_succeeded_without_a_report_says_the_report_is_missing(compose):
    _, appended = compose(SA_RUN_OUTCOME='success')

    assert first_line(appended) == '## Plurai simulation · ⛔ no report: `plurai ci run` finished but left no report'
    assert 'ended without writing' not in appended


def test_a_run_that_failed_without_a_report_keeps_the_ended_without_one_wording(compose):
    _, appended = compose(SA_RUN_OUTCOME='failure')

    assert 'ended without writing one' in first_line(appended)


def test_the_comment_of_a_run_that_never_started_says_so(compose, out_dir):
    compose('comment', SA_RUN_OUTCOME='skipped', SA_INSTALL_OUTCOME='failure')

    body = (out_dir / 'comment-final.md').read_text()
    assert body.startswith(f'{MARKER}\n## Plurai simulation · ⛔ not measured: the run did not start')
    assert 'the SDK install' in body
