"""Delivered bytes only: no browser, paid actions or real Zotero writes."""
import base64
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from save_received_blob import save_received


class ReceivedBlobTests(unittest.TestCase):
    def setUp(self):
        parent = Path('_work/pdf_pipeline_tests')
        parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=parent))
        self.url = 'https://www.ablesci.com/assist/download?id=test'
        self.adapter = Mock()
        self.adapter.collect_local.return_value = None
        self.adapter.job.return_value = {'download_attempt': {'url': self.url}}
        self.state = {'complete': True, 'url': self.url}
        for target, value in [('AbleSci', self.adapter), ('transfer_state', self.state)]:
            p = patch('save_received_blob.' + target, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def test_existing_verified_file_uses_no_browser(self):
        self.adapter.collect_local.return_value = {'status': 'download_verified'}
        with patch('save_received_blob.chrome', side_effect=AssertionError('no browser')):
            self.assertEqual(save_received(self.root, 'doi', self.root)['status'], 'download_verified')

    def test_wrong_or_incomplete_transfer_refused(self):
        for state in ({'complete': False, 'url': self.url}, {'complete': True, 'url': 'different'}):
            with patch('save_received_blob.transfer_state', return_value=state), \
                 patch('save_received_blob.chrome', side_effect=AssertionError('no browser')):
                with self.assertRaisesRegex(RuntimeError, 'exact completed'):
                    save_received(self.root, 'doi', self.root)

    def test_pending_review_not_exported_again(self):
        self.adapter.job.return_value['download_review'] = {'reason': 'identity mismatch'}
        with patch('save_received_blob.chrome', side_effect=AssertionError('no browser')):
            self.assertEqual(save_received(self.root, 'doi', self.root)['status'], 'download_needs_review')

    def run_payload(self, content, verified=False):
        data = 'data:application/pdf;base64,' + base64.b64encode(content).decode()
        answers = [{'status': 'ready', 'length': len(data)}]
        answers += [data[i:i+131072] for i in range(0, len(data), 131072)]
        if verified:
            self.adapter.collect_local.side_effect = [None, {'status': 'download_verified'}]
        with patch('save_received_blob.chrome', return_value={'started': True}) as browser, \
             patch('save_received_blob.read_chrome', side_effect=answers):
            result = save_received(self.root, 'doi', self.root)
        self.assertEqual(browser.call_count, 2)  # initialize owned cache; clean it
        return result

    def test_chunked_bytes_saved_exactly_then_identity_checked(self):
        content = b'%PDF-1.7\n' + b'x' * 200000
        self.assertEqual(self.run_payload(content, True)['status'], 'download_verified')
        expected = self.root / ('scansci-' + hashlib.sha256(content).hexdigest()[:20] + '.pdf')
        self.assertEqual(expected.read_bytes(), content)
        self.assertEqual(self.adapter.collect_local.call_count, 2)

    def test_pdf_header_does_not_replace_identity_validation(self):
        result = self.run_payload(b'%PDF-1.7\nnot a verified paper')
        self.assertEqual(result['status'], 'saved_needs_review')
        self.adapter.accept_verified.assert_not_called()

    def test_html_not_saved(self):
        with self.assertRaisesRegex(RuntimeError, 'not a PDF'):
            self.run_payload(b'<html>login</html>')
        self.assertEqual(list(self.root.glob('*.pdf')), [])

    def test_empty_start_response_is_not_replayed(self):
        with patch('save_received_blob.chrome', side_effect=[RuntimeError('empty'), {}]) as browser:
            with self.assertRaisesRegex(RuntimeError, 'empty'):
                save_received(self.root, 'doi', self.root)
        self.assertEqual(browser.call_count, 2)  # cleanup, never replay initialization
        self.assertTrue(browser.call_args.args[0].startswith('delete globalThis['))

    def test_oversized_cache_rejected_before_reading_bytes(self):
        with patch('save_received_blob.chrome', return_value={}), \
             patch('save_received_blob.read_chrome', return_value={'status': 'ready', 'length': 100000000}) as read:
            with self.assertRaisesRegex(RuntimeError, 'Invalid blob export length'):
                save_received(self.root, 'doi', self.root)
        self.assertEqual(read.call_count, 1)


if __name__ == '__main__':
    unittest.main()
