"""assert_check.py: the fixture's check on the run the reporter created."""

from __future__ import annotations

import io
import json

import assert_check
import pytest


def run_with(monkeypatch, runs, *args):
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(runs)))
    return assert_check.main(['--conclusion', 'failure', '--title', '1 passed, 1 failed and 0 skipped', *args])


def check(conclusion='failure', title='1 passed, 1 failed and 0 skipped ', summary='refunded 900 on turn 3'):
    return {'conclusion': conclusion, 'output': {'title': title, 'summary': summary}}


def test_one_run_that_matches_passes(monkeypatch):
    assert run_with(monkeypatch, [check()], '--contains', 'refunded 900') == 0


@pytest.mark.parametrize('runs', [[], [check(), check()]])
def test_no_run_and_two_runs_fail(monkeypatch, capsys, runs):
    assert run_with(monkeypatch, runs) == 1
    assert 'expected one check run' in capsys.readouterr().err


def test_a_green_conclusion_fails(monkeypatch, capsys):
    assert run_with(monkeypatch, [check(conclusion='success')]) == 1
    assert 'conclusion' in capsys.readouterr().err


def test_a_summary_without_the_judges_reason_fails(monkeypatch, capsys):
    assert run_with(monkeypatch, [check(summary='refund_flow')], '--contains', 'refunded 900') == 1
    assert 'refunded 900' in capsys.readouterr().err
