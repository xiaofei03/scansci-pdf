import hashlib
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from assist_bot import (Halt, Ledger, detail_page, eligible, multipart, request_id,
                        safe_host, scan_page, upload, STOP, Site, lookup_child, reconcile, authenticate,
                        require_pdf_runtime, BrowserSite)


def detail_html(state='waiting', note='', owner='OTHER', doi='10.1000/test'):
    return f'''<span class="assist-badge assist-badge-{state}">求助中</span>
    <table class="stable"><tr><td>标题</td><td class="assist-title">
    <a title="复制标题" data-clipboard-text="Example Study">copy</a></td></tr>
    <tr><td>DOI</td><td><div class="assist-doi">{doi}<button data-clipboard-text="{doi}">copy</button></div></td></tr>
    <tr><td>求助人</td><td><a class="show-user-tips" data-id="{owner}">User</a></td></tr>
    {('<tr><td>备注</td><td>'+note+'</td></tr>') if note else ''}</table>
    <input class="assist-id-val" value="ABC123">
    <meta name="csrf-token" content="secret-token">
    <script>new ss.SimpleUpload(); var x="/assist/upload-request";</script>'''


class BotTests(unittest.TestCase):
    def test_read_timeout_retries_once_with_safe_context(self):
        from unittest.mock import MagicMock
        site = Site(interval=0)
        site._call_once = MagicMock(side_effect=[TimeoutError('secret-body'), 'ok'])
        with patch('assist_bot.progress') as event:
            self.assertEqual(site.call('/assist/detail?id=ABC123'), 'ok')
        self.assertEqual(site._call_once.call_count, 2)
        self.assertNotIn('secret-body', str(event.call_args_list))

    def test_post_timeout_never_retries_or_prints_fields(self):
        from unittest.mock import MagicMock
        site = Site(interval=0)
        site._call_once = MagicMock(side_effect=TimeoutError('secret-body'))
        with patch('assist_bot.progress') as event:
            with self.assertRaisesRegex(Halt, '^site_timeout$'):
                site.call('/assist/upload-request?t=1', {'_csrf': 'private-token'})
        self.assertEqual(site._call_once.call_count, 1)
        self.assertNotIn('private-token', str(event.call_args_list))

    def test_browser_route_allowlist(self):
        for path, fields in [('/site/logout', None), ('//evil.example/', None),
                             ('https://www.ablesci.com/my/home', None),
                             ('/assist/create', {}), ('/assist/detail?id=ABC123', {}),
                             ('/assist/upload-request', None)]:
            with self.assertRaises(Halt):
                BrowserSite.check_path(path, fields)
        BrowserSite.check_path('/my/home', None)
        BrowserSite.check_path('/assist/upload-request?t=1', {})

    def test_browser_session_needs_no_password_prompt(self):
        with patch('assist_bot.sys.platform', 'darwin'):
            site = BrowserSite()
        with patch.object(site, 'confirm_session') as confirm, patch('builtins.input') as prompt:
            authenticate(site, False)
        confirm.assert_called_once()
        prompt.assert_not_called()

    def test_account_identity_must_match_before_upload(self):
        from unittest.mock import MagicMock
        site = Site(interval=0)
        site.call = MagicMock(return_value='<a href="/user/home?id=OTHER">My public profile</a>')
        with self.assertRaisesRegex(Halt, 'account_id_not_confirmed'):
            site.confirm_account('ME')
        site.call.return_value = '<a href="/user/home?id=ME">My public profile</a>'
        site.confirm_account('ME')

    def test_browser_response_and_cleanup_without_navigation(self):
        from unittest.mock import MagicMock
        with patch('assist_bot.sys.platform', 'darwin'):
            site = BrowserSite(interval=0)
        site.chrome = MagicMock(side_effect=['started', json.dumps({'done': True, 'text': 'article HTML'}), 'cleared'])
        with patch('assist_bot.STOP.wait', return_value=False):
            self.assertEqual(site.call('/assist/detail?id=ABC123'), 'article HTML')
        scripts = [c.args[0] for c in site.chrome.call_args_list]
        self.assertIn('credentials:"same-origin"', scripts[0])
        self.assertIn('delete window[k]', scripts[-1])
        for script in scripts:
            for forbidden in ('document.cookie', '.click(', 'location.href=', 'window.open('):
                self.assertNotIn(forbidden, script)

    def test_browser_pins_tab_ids_and_redacts_command_errors(self):
        from types import SimpleNamespace
        with patch('assist_bot.sys.platform', 'darwin'):
            site = BrowserSite()
        with patch('assist_bot.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='77\n88\nstarted\n')) as run:
            self.assertEqual(site.chrome('test-js'), 'started')
            self.assertEqual(site.target, ('77', '88'))
            site.chrome('poll-js')
            self.assertEqual(run.call_args.args[0][-3:], ['77', '88', 'poll-js'])
        with patch('assist_bot.subprocess.run', return_value=SimpleNamespace(returncode=1, stdout='', stderr='secret-token')):
            with self.assertRaisesRegex(Halt, '^browser_automation_unavailable$'):
                site.chrome('poll-js')

    def test_browser_post_timeout_cleans_up_and_never_redispatches(self):
        from unittest.mock import MagicMock
        with patch('assist_bot.sys.platform', 'darwin'):
            site = BrowserSite(interval=0)
        site.chrome = MagicMock(side_effect=['started', json.dumps({'done': True, 'error': 'timeout'}), 'cleared'])
        with patch('assist_bot.STOP.wait', return_value=False), patch('assist_bot.progress'):
            with self.assertRaisesRegex(Halt, '^site_timeout$'):
                site.call('/assist/upload-request', {'_csrf': 'private-token'})
        self.assertEqual(site.chrome.call_count, 3)
        self.assertIn('delete window[k]', site.chrome.call_args.args[0])

    def test_missing_pdf_runtime_fails_before_login(self):
        with patch('assist_bot.importlib.import_module', side_effect=ImportError):
            with self.assertRaisesRegex(Halt, 'pdf_runtime_missing'):
                require_pdf_runtime()

    def test_pdf_runtime_version_range(self):
        from types import SimpleNamespace
        for version in ('6.9.0', '7.0.0', 'unknown'):
            with patch('assist_bot.importlib.import_module', return_value=SimpleNamespace(__version__=version)):
                with self.assertRaises(Halt):
                    require_pdf_runtime()
        with patch('assist_bot.importlib.import_module', return_value=SimpleNamespace(__version__='6.10.0')):
            self.assertEqual(require_pdf_runtime(), {'pypdf_version': '6.10.0'})

    def tearDown(self):
        STOP.clear()

    def test_queue_resumes_ready_items_not_on_first_page(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(folder)
            ledger.save('ABC123', 'ready', {})
            ledger.save('POSTED', 'posting_uncertain', {})
            fresh = [{'id': 'FRESH1'}, {'id': 'ABC123'}, {'id': 'POSTED'}]
            self.assertEqual([r['id'] for r in ledger.candidates(fresh, True, 10)], ['ABC123', 'FRESH1'])
            self.assertEqual(ledger.candidates(fresh, False, 10), [{'id': 'FRESH1'}])
            self.assertEqual(ledger.candidates([], True, 10), [{'id': 'ABC123'}])
            ledger.db.close()

    def test_queue_retry_backoff_without_resetting_write_states(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(folder)
            for ident, state in [('FAILED', 'lookup_failed'), ('TIMEOUT', 'lookup_timeout'),
                                 ('UPLOAD', 'uploaded'), ('UNCERT', 'posting_uncertain')]:
                ledger.save(ident, state, {})
            now = time.time()
            self.assertEqual(ledger.candidates([], True, 10, now), [])
            self.assertEqual({r['id'] for r in ledger.candidates([], True, 10, now, retry_failed=True)}, {'FAILED', 'TIMEOUT'})
            self.assertEqual({r['id'] for r in ledger.candidates([], True, 10, now + 21601)}, {'FAILED', 'TIMEOUT'})
            ledger.save('DEFER1', 'run_deadline', {})
            self.assertEqual(ledger.candidates([], True, 10, now), [{'id': 'DEFER1'}])
            ledger.db.close()

    def test_login_requires_positive_authenticated_page_evidence(self):
        from unittest.mock import MagicMock
        site = Site(interval=0)
        site.call = MagicMock(side_effect=['<meta name="csrf-token" content="token">', {'code': 0}, '<html>maintenance</html>'])
        with self.assertRaisesRegex(Halt, 'login_not_confirmed'):
            site.login('user', 'not-a-real-password')
        site.call = MagicMock(side_effect=['<meta name="csrf-token" content="token">', {'code': 0}, '<a href="/site/logout">退出</a>'])
        site.login('user', 'not-a-real-password')

    def test_login_prompt_cannot_fall_back_to_echoed_pipe(self):
        with patch('sys.stdin.isatty', return_value=False), patch.dict('os.environ', {'ABLESCI_PASSWORD': 'test-secret'}):
            with self.assertRaisesRegex(Halt, 'private_interactive_terminal'):
                authenticate(None, True)
            import os
            self.assertNotIn('ABLESCI_PASSWORD', os.environ)

    def test_real_lookup_child_terminated_on_stop(self):
        original_popen = subprocess.Popen
        children = []
        def spawn(*args, **kwargs):
            child = original_popen([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
            children.append(child)
            return child
        timer = threading.Timer(0.1, STOP.set)
        timer.start()
        try:
            with patch('assist_bot.subprocess.Popen', side_effect=spawn):
                with self.assertRaisesRegex(Halt, 'stopped'):
                    lookup_child(Path('/unused'), '10.1000/test', time.monotonic() + 10)
            self.assertTrue(children)
            self.assertIsNotNone(children[0].poll())
        finally:
            timer.join()
            for child in children:
                if child.poll() is None:
                    child.kill(); child.wait()

    def test_real_lookup_child_timeout_reaped(self):
        original_popen = subprocess.Popen
        children = []
        def spawn(*args, **kwargs):
            child = original_popen([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
            children.append(child)
            return child
        with patch('assist_bot.subprocess.Popen', side_effect=spawn):
            result = lookup_child(Path('/unused'), '10.1000/test', time.monotonic() + 0.2)
        self.assertEqual(result['status'], 'run_deadline')
        self.assertIsNotNone(children[0].poll())

    def test_reconcile_is_read_only_and_does_not_invent_acceptance(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(folder)
            ledger.save('ABC123', 'posting_uncertain', {'artifact': {'doi': '10.1000/test'}})
            site = MagicMock()
            site.detail.return_value = detail_page(detail_html(state='completed'), 'ABC123')
            observations = reconcile(site, ledger)
            self.assertEqual(ledger.state('ABC123')[0], 'posting_uncertain')
            self.assertEqual(observations[0]['acceptance'], 'not_verified')
            self.assertIsNone(observations[0]['points_earned'])
            self.assertTrue(observations[0]['doi_matches'])
            site.call.assert_not_called()
            ledger.db.close()

    def test_detail_identity_and_note(self):
        item = detail_page(detail_html(), 'ABC123')
        self.assertEqual(item['doi'], '10.1000/test')
        self.assertEqual(item['title'], 'Example Study')
        self.assertTrue(item['upload_form'])
        self.assertTrue(eligible(item, 'ME'))
        self.assertFalse(eligible(detail_page(detail_html(owner='ME'), 'ABC123'), 'ME'))
        self.assertFalse(eligible(detail_page(detail_html(note='Need supplement'), 'ABC123'), 'ME'))
        self.assertFalse(eligible(detail_page(detail_html(state='uploaded'), 'ABC123'), 'ME'))

    def test_wrong_id_and_ambiguous_doi(self):
        with self.assertRaises(Halt):
            detail_page(detail_html(), 'DIFFERENT')
        d = detail_page(detail_html().replace('data-clipboard-text="10.1000/test"', 'data-other="x"'), 'ABC123')
        self.assertFalse(eligible(d, 'ME'))

    def test_list_skips_pinned_and_uploaded(self):
        html = ''.join(f'<h2 class="assist-list-title"><a class="{cls}" href="/assist/detail?id={ident}">{ident}</a></h2>'
                       for cls, ident in [('', 'ABC123'), ('stick-assist', 'PIN123'), ('high-point-uploaded', 'DONE12'), ('', 'ABC123')])
        self.assertEqual(scan_page(html), [{'id': 'ABC123', 'title': 'ABC123'}])

    def test_external_id_rejected(self):
        for url in ('https://evil.test/assist/detail?id=ABC123', '//evil.test/assist/detail?id=ABC123',
                    '/assist/detail?id=ABC123&id=XYZ123', '/assist/detail?id=../../etc'):
            self.assertIsNone(request_id(url))

    def test_nonpublic_storage_host(self):
        with self.assertRaises(Halt):
            safe_host('http://bucket.example/', set())
        with self.assertRaises(Halt):
            safe_host('https://unknown.example/', {'allowed.example'})
        with patch('socket.getaddrinfo', return_value=[(None, None, None, None, ('127.0.0.1', 443))]):
            with self.assertRaises(Halt):
                safe_host('https://bucket.example/', set())

    def test_multipart_preserves_original_bytes(self):
        raw = b'%PDF-1.7\n\x00\xff\n'
        body, typ = multipart({'x:assist_id': 'ABC123'}, 'article-abcd.pdf', raw)
        self.assertIn(raw, body)
        self.assertIn(b'name="x:assist_id"', body)
        self.assertIn('boundary=', typ)

    def test_ledger_keeps_write_before_network_and_no_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(folder)
            ledger.save('ABC123', 'posting_uncertain', {'csrf': 'secret', 'item': {'csrf': 'nested-secret'}, 'title': 'Study'})
            self.assertEqual(ledger.attempted_today(), 1)
            self.assertNotIn('secret', ledger.db.execute('SELECT data FROM jobs').fetchone()[0])
            ledger.save('ABC123', 'uploaded', {})
            self.assertEqual(ledger.attempted_today(), 1)
            ledger.db.close()

    def run_upload(self, code, changed=False):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        path = root / 'test.pdf'
        path.write_bytes(b'%PDF-' + b'x' * 2000)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        artifact = dict(doi='10.1000/test', pdf=str(path), sha256='bad' if changed else sha,
                        metadata={'title': 'Example Study'}, license={'license_url': 'https://creativecommons.org/licenses/by/4.0/'})
        ledger = Ledger(root)
        self.addCleanup(ledger.db.close)
        item = detail_page(detail_html(), 'ABC123')

        class FakeSite:
            called = 0
            def detail(self, ident):
                return item
            def call(self, path, fields):
                self.called += 1
                if ledger.state('ABC123')[0] != 'posting_uncertain':
                    raise AssertionError('network started without uncertainty checkpoint')
                if code == 'timeout':
                    raise TimeoutError()
                return {'code': code}
        site = FakeSite()
        with patch('pipeline.validate_pdf', return_value={'status': 'verified'}):
            if changed:
                with self.assertRaises(Halt):
                    upload(site, ledger, item, artifact, 'ME', set())
                self.assertEqual(site.called, 0)
                return
            if code == 10:
                upload(site, ledger, item, artifact, 'ME', set())
                self.assertEqual(ledger.state('ABC123')[0], 'uploaded')
            else:
                with self.assertRaises((Halt, TimeoutError)):
                    upload(site, ledger, item, artifact, 'ME', set())
                self.assertEqual(ledger.state('ABC123')[0], 'posting_uncertain')
            with self.assertRaises(Halt):
                upload(site, ledger, item, artifact, 'ME', set())
            self.assertEqual(site.called, 1)

    def test_md5_dedup_is_already_a_committing_write(self):
        self.run_upload(10)

    def test_captcha_stops_without_retry(self):
        self.run_upload(2)

    def test_timeout_is_not_reposted(self):
        self.run_upload('timeout')

    def test_changed_pdf_not_uploaded(self):
        self.run_upload(10, changed=True)

    def test_storage_handshake_does_not_forward_site_credentials(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'article.pdf'
            path.write_bytes(b'%PDF-' + b'x' * 2000)
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            artifact = dict(doi='10.1000/test', pdf=str(path), sha256=sha,
                            metadata={'title': 'Example Study'}, license={'license_url': 'cc'})
            item = detail_page(detail_html(), 'ABC123')
            site = MagicMock()
            site.detail.return_value = item
            site.call.return_value = {'code': 0, 'data': dict(host='https://storage.example/',
                dir='folder/', randFilename='file.pdf', policy='signed-policy', accessid='signed-access',
                callback='signed-callback', signature='signed-signature', filename='article.pdf',
                assist_id=123, user_id=456)}
            ledger = Ledger(root)
            response = MagicMock()
            response.__enter__.return_value.read.return_value = b'{"code":0}'
            opener = MagicMock()
            opener.open.return_value = response
            with patch('pipeline.validate_pdf', return_value={'status': 'verified'}), \
                 patch('assist_bot.safe_host', return_value='https://storage.example/'), \
                 patch('assist_bot.request.build_opener', return_value=opener):
                upload(site, ledger, item, artifact, 'ME', {'storage.example'})
            req = opener.open.call_args.args[0]
            self.assertNotIn(b'secret-token', req.data)
            self.assertFalse(req.has_header('Cookie'))
            self.assertFalse(req.has_header('Authorization'))
            self.assertIn(b'name="x:user_id"\r\n\r\n456', req.data)
            self.assertEqual(ledger.state('ABC123')[0], 'uploaded')
            self.assertNotIn('signed-policy', ledger.db.execute('SELECT data FROM jobs').fetchone()[0])
            self.assertNotIn('secret-token', ledger.db.execute('SELECT data FROM jobs').fetchone()[0])
            ledger.db.close()


if __name__ == '__main__':
    unittest.main()
