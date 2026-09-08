#!/usr/bin/env python3
"""Read-only setup checks. Never installs, logs in, changes prefs or prints secrets."""
import argparse
import importlib.util
import json
import platform
import shutil
import urllib.request
from pathlib import Path


def probe(route):
    try:
        with urllib.request.urlopen('http://127.0.0.1:23119' + route, timeout=3) as response:
            response.read(1024)
            return response.status == 200
    except Exception:
        return False


def inspect(prefs=None):
    mac = platform.system() == 'Darwin'
    base = Path.home() / 'Library/Application Support/Zotero/Profiles'
    candidates = sorted(str(p) for p in base.glob('*/prefs.js')) if mac else []
    return dict(
        platform=platform.system(), python=platform.python_version(),
        desktop_adapter_supported=mac, pypdf_available=importlib.util.find_spec('pypdf') is not None,
        apple_events_available=shutil.which('osascript') is not None,
        zotero_connector_responding=probe('/connector/ping'),
        zotero_local_api_responding=probe('/api/users/0/collections?limit=1'),
        requested_prefs_exists=Path(prefs).expanduser().is_file() if prefs else None,
        prefs_candidates=candidates,
        manual_checks=['Zotero selected personal-library collection',
                       'macOS Automation/Accessibility permission and unlocked desktop',
                       'Chrome logged in with one AbleSci tab and Apple Events JavaScript allowed',
                       'Browser download directory; explicit point budgets for new requests'],
        note='Read-only checks do not certify UI permissions, account access or successful downloads.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--prefs')
    args = parser.parse_args()
    print(json.dumps(inspect(args.prefs), ensure_ascii=False, indent=2))
