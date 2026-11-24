"""assert_comment.py: counts the action's sticky comments for one workflow key."""

from __future__ import annotations

import io
import json

import pytest
from assert_comment import main

BOT = {'login': 'github-actions[bot]'}


def feed(monkeypatch, comments):
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps([comments])))


def comment(key, body='x', user=BOT):
    return {'user': user, 'body': f'<!-- plurai-simulation-report:workflow={key} -->\n{body}'}


def test_one_comment_for_the_key_passes(monkeypatch):
    feed(monkeypatch, [comment('a'), comment('b')])

    assert main(['--key', 'a', '--count', '1']) == 0


def test_two_comments_for_the_key_fail(monkeypatch):
    feed(monkeypatch, [comment('a'), comment('a')])

    assert main(['--key', 'a', '--count', '1']) == 1


def test_a_comment_by_someone_else_does_not_count(monkeypatch):
    feed(monkeypatch, [comment('a', user={'login': 'mallory'})])

    assert main(['--key', 'a', '--count', '0']) == 0


@pytest.mark.parametrize(('needle', 'code'), [('second run', 0), ('first run', 1)])
def test_the_latest_body_is_checked(monkeypatch, needle, code):
    feed(monkeypatch, [comment('a', 'second run')])

    assert main(['--key', 'a', '--count', '1', '--contains', needle]) == code
