"""No browser, permission changes, library writes or real spending in these tests."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from doctor import download_preflight
from batch import main, completion_exit_code


class DownloadPreflightTests(unittest.TestCase):
    def setUp(self):
        parent = Path('_work/pdf_pipeline_tests')
        parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=parent)).resolve()
        self.prefs = self.root / 'Preferences'
        self.data = {'download': {'default_directory': str(self.root)},
                     'profile': {'content_settings': {'exceptions': {'automatic_downloads': {
                         'https://www.ablesci.com,*': {'setting': 1}}}}}}

    def check(self):
        self.prefs.write_text(json.dumps(self.data), encoding='utf-8')
        before = self.prefs.read_bytes()
        result = download_preflight(self.prefs, self.root)
        self.assertEqual(before, self.prefs.read_bytes())
        return result

    def test_exact_site_allow_passes_without_modification(self):
        self.assertTrue(self.check()['ready'])

    def test_no_profile_is_not_guessed(self):
        self.assertIn('explicit_chrome_preferences_required', download_preflight(None, self.root)['issues'])

    def test_global_allow_is_not_scoped_permission(self):
        self.data['profile']['content_settings'] = {'exceptions': {}, 'default_content_setting_values': {'automatic_downloads': 1}}
        self.assertFalse(self.check()['ready'])

    def test_blocked_or_expiring_site_is_unready(self):
        for entry in ({'setting': 2}, {'setting': 1, 'expiration': '1234'}):
            self.data['profile']['content_settings']['exceptions']['automatic_downloads']['https://www.ablesci.com,*'] = entry
            self.assertFalse(self.check()['ready'])

    def test_save_as_and_wrong_directory_are_reported(self):
        self.data['download'] = {'prompt_for_download': True, 'default_directory': str(self.root / 'other')}
        self.assertEqual(set(self.check()['issues']), {'save_as_prompt_enabled', 'chrome_download_directory_mismatch'})

    def test_corrupt_preferences_fail_closed(self):
        self.prefs.write_text('{', encoding='utf-8')
        self.assertFalse(download_preflight(self.prefs, self.root)['ready'])

    def test_unready_cli_exits_before_creating_batch_or_writing(self):
        argv = ['batch.py', '--collection', 'TEST', '--work-dir', str(self.root),
                '--prefs', str(self.root / 'prefs.js'), '--ablesci', '--downloads-dir', str(self.root)]
        with patch('sys.argv', argv), patch('sys.platform', 'darwin'), \
             patch('batch.Batch', side_effect=AssertionError('must not start library work')):
            self.assertEqual(main(), 2)

    def test_preflight_only_never_starts_batch_even_when_ready(self):
        self.check()
        argv = ['batch.py', '--collection', 'TEST', '--work-dir', str(self.root),
                '--prefs', str(self.root / 'prefs.js'), '--preflight-only',
                '--chrome-preferences', str(self.prefs), '--downloads-dir', str(self.root)]
        with patch('sys.argv', argv), patch('sys.platform', 'darwin'), \
             patch('batch.Batch', side_effect=AssertionError('must not write')):
            self.assertEqual(main(), 0)

    def test_completion_codes_distinguish_success_review_wait(self):
        doi = '10.1234/test'
        self.assertEqual(completion_exit_code([{'doi':doi, 'status':'complete'}], [doi]), 0)
        self.assertEqual(completion_exit_code([{'doi':doi, 'status':'needs_ablesci'}], [doi], needs_review=True), 2)
        self.assertEqual(completion_exit_code([{'doi':doi, 'status':'needs_ablesci'}], [doi]), 3)
        self.assertEqual(completion_exit_code([{'doi':doi, 'status':'complete', 'source':'ablesci'}], [doi], accept=True), 3)


if __name__ == '__main__':
    unittest.main()
