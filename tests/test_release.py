"""release.json: one pin per release, installed from PyPI, and nothing of any other install source anywhere in the tree."""

from __future__ import annotations

import json
import subprocess

import install_sdk
from conftest import REPO

RELEASE = json.loads((REPO / 'release.json').read_text())
PRIVATE_MARKERS = (
    'pkg.dev',
    'prompt-optimization',
    'mint',
    'mint_index_token',
    'private-index',
    'private index',
    'private_index',
    'pypi-reader',
    'SA_INDEX_TOKEN',
    'pre-launch',
    'prelaunch',
    'google-github-actions',
    'workload identity',
    'workload_identity',
)


def test_the_pin_is_one_exact_version_the_range_rule_can_read():
    sdk = RELEASE['sdk']

    assert not any(ch in sdk['version'] for ch in '<>=~^*, ')
    assert install_sdk.parse_version(sdk['version'])
    assert sdk['source'] == 'pypi'


def test_the_pin_asks_for_no_extras():
    assert set(RELEASE['sdk']) <= {'version', 'source'}


def test_the_managed_rev_is_a_positive_integer():
    assert isinstance(RELEASE['managed_rev'], int)
    assert RELEASE['managed_rev'] >= 1


def tracked_files() -> list[str]:
    listing = subprocess.run(
        ['git', 'ls-files', '-co', '--exclude-standard'], cwd=REPO, capture_output=True, text=True, check=True
    )
    return [name for name in listing.stdout.splitlines() if (REPO / name).is_file()]


def test_the_tree_carries_no_reference_to_another_install_source():
    own = {'tests/test_release.py'}
    offenders = [
        name
        for name in tracked_files()
        if name not in own and any(marker in (REPO / name).read_text(errors='replace') for marker in PRIVATE_MARKERS)
    ]

    assert not offenders
    assert not (REPO / 'docs' / 'pre-launch.md').exists()
