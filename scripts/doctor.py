#!/usr/bin/env python3
"""Read-only setup checks. Never installs, logs in, changes prefs or prints secrets."""
import argparse
import importlib.util
import json
import platform
import shutil
import os
import urllib.request
from pathlib import Path


def download_preflight(preferences, downloads_dir):
    """Check local export destination; optionally diagnose native browser saving.

    Delivered-blob export does not use Chrome's download permission or Save As.
    An explicit profile opts into the older native-save diagnostic only.
    """
    issues = []
    root = Path(downloads_dir).expanduser().resolve() if downloads_dir else None
    if not root or not root.is_dir() or not os.access(root, os.W_OK):
        issues.append('download_directory_missing_or_not_writable')
    if not preferences:
        return {'ready': not issues, 'issues': issues, 'save_method': 'delivered_blob_export'}
    try:
        data = json.loads(Path(preferences).expanduser().read_text(encoding='utf-8'))
        download = data.get('download', {})
        exceptions = data.get('profile', {}).get('content_settings', {}).get('exceptions', {}).get('automatic_downloads', {})
        if not isinstance(download, dict) or not isinstance(exceptions, dict):
            raise ValueError('unsupported preferences')
        # Require a permanent allow for this exact site, not a global relaxation.
        allowed = False
        for origin in ('https://www.ablesci.com,*', 'https://www.ablesci.com:443,*'):
            entry = exceptions.get(origin, {})
            if not isinstance(entry, dict):
                continue
            if entry.get('setting') == 1 and str(entry.get('expiration', '0')) == '0':
                allowed = True
        if not allowed:
            issues.append('ablesci_multiple_downloads_not_explicitly_allowed')
        if download.get('prompt_for_download', False):
            issues.append('save_as_prompt_enabled')
        configured = Path(download.get('default_directory') or (Path.home() / 'Downloads')).expanduser().resolve()
        if root and configured != root:
            issues.append('chrome_download_directory_mismatch')
    except (OSError, ValueError, TypeError, AttributeError):
        issues.append('chrome_preferences_unreadable_or_unsupported')
    return {'ready': not issues, 'issues': issues,
            'note': 'Read-only disk check; selected profile must match the AbleSci tab. Runtime/policy overrides remain possible.'}


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
    parser.add_argument('--chrome-preferences')
    parser.add_argument('--downloads-dir')
    args = parser.parse_args()
    result = inspect(args.prefs)
    if args.chrome_preferences or args.downloads_dir:
        result['download_preflight'] = download_preflight(args.chrome_preferences, args.downloads_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
