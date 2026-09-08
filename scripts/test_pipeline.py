import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter
from reportlab.pdfgen import canvas

import pipeline
from ablesci import AbleSci, guard
from zotero_gui import execute
from batch import finish_ablesci, run


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path('_work/pdf_pipeline_tests')
        root.mkdir(parents=True, exist_ok=True)
        cls.root = Path(tempfile.mkdtemp(dir=root))
        cls.meta = dict(title='A specific research title', doi='10.1234/example', authors=['Smith'], page_range='1-2')

    def pdf(self, name, title=None, doi=None, author='Smith', pages=2):
        path = self.root / name
        c = canvas.Canvas(str(path))
        for n in range(pages):
            c.drawString(50, 780, title or self.meta['title'])
            c.drawString(50, 760, doi or self.meta['doi'])
            c.drawString(50, 740, author)
            for y in range(710, 100, -20):
                c.drawString(50, y, 'Research methods results discussion evidence. ' * 2)
            c.showPage()
        c.save()
        return path

    def test_identity(self):
        self.assertEqual(pipeline.validate_pdf(self.pdf('good.pdf'), self.meta)['status'], 'verified')

    def test_wrong_title_despite_doi(self):
        self.assertEqual(pipeline.validate_pdf(self.pdf('wrong.pdf', title='A different paper'), self.meta)['status'], 'needs_review')

    def test_wrong_author(self):
        self.assertEqual(pipeline.validate_pdf(self.pdf('author.pdf', author='Jones'), self.meta)['status'], 'needs_review')

    def test_one_page(self):
        self.assertEqual(pipeline.validate_pdf(self.pdf('one.pdf', pages=1), self.meta)['status'], 'rejected')

    def test_known_page_range(self):
        m = dict(self.meta, page_range='20-25')
        self.assertEqual(pipeline.validate_pdf(self.pdf('short.pdf'), m)['reason'], 'fewer_pages_than_published_range')

    def test_html(self):
        path = self.root / 'login.pdf'
        path.write_text('<html>Login</html>' * 100)
        self.assertEqual(pipeline.validate_pdf(path, self.meta)['reason'], 'not_pdf')

    def test_encryption(self):
        path = self.root / 'encrypted.pdf'
        w = PdfWriter()
        w.add_blank_page(595, 842)
        w.encrypt('test-password')
        w.write(str(path))
        self.assertEqual(pipeline.validate_pdf(path, self.meta)['status'], 'rejected')

    def test_linked_not_managed(self):
        self.assertFalse(pipeline.good([dict(status='verified', managed=False)]))

    def test_normalize(self):
        self.assertEqual(pipeline.doi_normalize('https://doi.org/10.1000/AbC'), '10.1000/abc')
        with self.assertRaises(ValueError):
            pipeline.doi_normalize('not a DOI')

    def test_custom_resolver_guard(self):
        p = self.root / 'prefs.js'
        for automatic in (False, True):
            value = json.dumps(json.dumps([dict(automatic=automatic)]))
            p.write_text('user_pref("extensions.zotero.findPDFs.resolvers", ' + value + ');')
            if automatic:
                with self.assertRaises(RuntimeError):
                    pipeline.safe_resolver_prefs(p)
            else:
                pipeline.safe_resolver_prefs(p)

    def test_budget_before_browser(self):
        adapter = AbleSci(self.root)
        with patch('ablesci.snapshot', side_effect=AssertionError('must not access browser')):
            with self.assertRaisesRegex(RuntimeError, 'not authorized'):
                adapter.submit('10.1234/example', 0, 0)

    def test_captcha(self):
        with self.assertRaises(RuntimeError):
            guard(dict(url='https://www.ablesci.com/assist/create', text='请完成安全验证'))

    def test_upsert_no_duplicate(self):
        b = pipeline.Batch(self.root, 'TEST', self.root / 'prefs.js')
        for state in ('new', 'needs_ablesci'):
            b.save(dict(doi='10.1234/example', status=state))
        self.assertEqual(b.db.execute('SELECT count(*) FROM jobs WHERE doi=?', ('10.1234/example',)).fetchone()[0], 1)

    def test_publisher_metadata(self):
        parser = pipeline.CitationMetadata()
        parser.feed('<meta name="citation_title" content="A &amp; B"><meta name="citation_pdf_url" content="/paper.pdf">')
        self.assertEqual(parser.values['citation_title'], 'A & B')
        self.assertEqual(parser.values['citation_pdf_url'], '/paper.pdf')

    def test_empty_attach_no_ui(self):
        self.assertEqual(execute(None, 'attach', [])['status'], 'nothing_to_do')

    def test_attach_stream_scoped(self):
        job = dict(key='TEST', pdf=str(self.pdf('stream.pdf')), metadata=self.meta)
        def inspect(batch, action, jobs, wait, streams):
            import urllib.request
            url = streams['TEST']
            self.assertTrue(url.startswith('http://127.0.0.1:'))
            self.assertEqual(urllib.request.urlopen(url).read()[:5], b'%PDF-')
            with self.assertRaises(Exception):
                urllib.request.urlopen(url.rsplit('/', 1)[0] + '/other.pdf')
            return {'status': 'tested'}
        with patch('zotero_gui._execute', side_effect=inspect):
            self.assertEqual(execute(None, 'attach', [job])['status'], 'tested')

    def test_resume_missing_skips_repeated_lookup(self):
        b = pipeline.Batch(self.root, 'TEST', self.root / 'prefs.js')
        b.save(dict(doi='10.1234/resume', status='needs_ablesci', metadata=self.meta))
        with patch('batch.execute', side_effect=AssertionError('must not open UI')):
            run(b, ['10.1234/resume'])
        row = json.loads(b.db.execute('SELECT data FROM jobs WHERE doi=?', ('10.1234/resume',)).fetchone()[0])
        self.assertEqual(row['status'], 'needs_ablesci')

    def test_orchestrator_budget_gate(self):
        b = pipeline.Batch(self.root, 'TEST', self.root / 'prefs.js')
        b.save(dict(doi='10.1234/budget', status='needs_ablesci', metadata=self.meta))
        with patch('ablesci.snapshot', side_effect=AssertionError('must not open browser')):
            finish_ablesci(b, ['10.1234/budget'], self.root)

    def test_download_resume_no_browser(self):
        adapter = AbleSci(self.root)
        job = dict(source='ablesci', pdf=str(self.pdf('resume.pdf')), metadata=self.meta)
        with patch.object(adapter, 'job', return_value=job), patch('ablesci.snapshot', side_effect=AssertionError('no browser')):
            self.assertEqual(adapter.download('10.1234/example', self.root)['status'], 'download_verified')


if __name__ == '__main__':
    unittest.main(verbosity=2)
