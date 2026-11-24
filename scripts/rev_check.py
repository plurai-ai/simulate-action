"""Warn when the calling workflow's managed block is older than this release expects.

The workflow's first lines carry ``# plurai:managed rev=<n>``; ``release.json`` names the ``managed_rev`` this release
expects. A workflow without the marker was not rendered by ``/simulation-ci`` and is left alone.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

import common

MARKER = re.compile(r'^#\s*plurai:managed\s+rev=(\d+)\b')
#: The marker is the workflow's first comment line; later lines are the user's.
LINES_SCANNED = 5
DEFAULT_RELEASE_FILE = Path(__file__).resolve().parent.parent / 'release.json'


def release_file(env: Mapping[str, str]) -> Path:
    return Path(env.get('SA_RELEASE_FILE') or DEFAULT_RELEASE_FILE)


def expected_rev(env: Mapping[str, str]) -> int:
    return int(json.loads(release_file(env).read_text(encoding='utf-8'))['managed_rev'])


def workflow_rev(env: Mapping[str, str]) -> int | None:
    ref = env.get('GITHUB_WORKFLOW_REF', '')
    marker = '/.github/workflows/'
    if marker not in ref:
        return None
    relative = '.github/workflows/' + ref.split(marker, 1)[1].split('@', 1)[0]
    path = Path(env.get('GITHUB_WORKSPACE') or '.') / relative
    try:
        head = path.read_text(encoding='utf-8').splitlines()[:LINES_SCANNED]
    except OSError:
        return None
    for line in head:
        match = MARKER.match(line)
        if match:
            return int(match.group(1))
    return None


def main(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    found = workflow_rev(env)
    if found is None:
        return common.OK
    expected = expected_rev(env)
    if found < expected:
        text = (
            f"The workflow's managed block is rev={found}; this release of the action expects rev={expected}. "
            'Re-render it with `/simulation-ci`.'
        )
        common.warning(text)
        common.add_note(common.out_dir(env), text)
    return common.OK


if __name__ == '__main__':
    sys.exit(main())
