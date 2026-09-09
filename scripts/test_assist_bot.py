import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from assist_bot import (Halt, Ledger, detail_page, eligible, multipart, request_id,
                        safe_host, scan_page, upload)


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
