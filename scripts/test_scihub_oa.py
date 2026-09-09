import copy
import io
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.request import Request

from reportlab.pdfgen import canvas
from free_sources import FreeSources, SafeRedirect, source_url
from scihub_oa import HOST, SOURCE, license_evidence


class SupplementTests(unittest.TestCase):
    def setUp(self):
        root=Path('_work/scihub_oa_tests'); root.mkdir(parents=True,exist_ok=True)
        self.root=Path(tempfile.mkdtemp(dir=root))
        self.engine=FreeSources(self.root,config={},scihub_oa=True)
        self.meta=dict(doi='10.1234/test',title='Research on organizations',authors=['Smith'],
                       page_range='1-2',url='https://publisher.example/article',links=[],
                       metadata_source='Crossref',licenses=[{'URL':'https://creativecommons.org/licenses/by/4.0',
                       'content-version':'vor','start':{'date-parts':[[2021,1,1]]}}])

    def pdf(self, doi='10.1234/test'):
        out=io.BytesIO(); c=canvas.Canvas(out)
        for _ in range(2):
            c.drawString(20,800,'Research on organizations '+doi+' Smith')
            for y in range(40,750,20): c.drawString(20,y,'Results methods discussion reference evidence. '*2)
            c.showPage()
        c.save(); return out.getvalue()

    def run_source(self,html=None,pdf=None):
        html=html if html is not None else b'<iframe src="/storage/a.pdf?download=1"></iframe>'
        with patch.object(self.engine,'fetch',side_effect=[(html,'https://'+HOST+'/article'),
                  (pdf if pdf is not None else self.pdf(),'https://'+HOST+'/storage/a.pdf')]) as call:
            result=self.engine.acquire_scihub_oa(self.meta)
        return result,call

    def test_license_positive(self):
        for suffix in ('licenses/by/4.0','licenses/by-sa/3.0','publicdomain/zero/1.0'):
            meta=copy.deepcopy(self.meta); meta['licenses'][0]['URL']='https://creativecommons.org/'+suffix
            self.assertEqual(license_evidence(meta)['content_version'],'vor')

    def test_license_negative_and_malformed(self):
        variants=[dict(self.meta,licenses=None),dict(self.meta,licenses=[]),dict(self.meta,metadata_source='other'),
                  dict(self.meta,licenses=['bad']),dict(self.meta,licenses={},is_oa=True)]
        for change in ({'content-version':'tdm'},{'content-version':'am'},
                       {'start':{'date-parts':[[2099,1,1]]}}, {'start':{}},
                       {'URL':'https://creativecommons.org.evil.test/licenses/by/4.0'},
                       {'URL':'https://creativecommons.org/licenses/by-nc/4.0'},
                       {'URL':'https://[invalid'}, {'URL':None}):
            meta=copy.deepcopy(self.meta); meta['licenses'][0].update(change); variants.append(meta)
        with patch.object(self.engine,'fetch',side_effect=AssertionError('no network')):
            for meta in variants:
                self.assertIsNone(license_evidence(meta))
                self.assertEqual(self.engine.acquire_scihub_oa(meta)['events'][0]['status'],'open_license_not_verified')

    def test_scope_on_initial_url_and_redirect(self):
        req=Request('https://'+HOST+'/a')
        for url in ('http://'+HOST+'/a','https://elsewhere.example/a','https://'+HOST+':444/a',
                    'https://user:pass@'+HOST+'/a','http://127.0.0.1/a'):
            with self.assertRaises(ValueError): source_url(url,{HOST})
            with self.assertRaises(ValueError): SafeRedirect({HOST}).redirect_request(req,None,302,'',{},url)
        source_url('https://'+HOST+'/a.pdf',{HOST})

    def test_credentials_removed_and_scope_forwarded(self):
        with patch('free_sources.download_bytes',return_value=(b'ok','https://'+HOST+'/a')) as call:
            self.engine.fetch(SOURCE,'https://'+HOST+'/a',{'Authorization':'secret','Cookie':'private'},time.monotonic()+5,[])
        self.assertEqual(call.call_args.args[1],{'Accept':'text/html'})
        self.assertEqual(call.call_args.kwargs['allowed_hosts'],{HOST})

    def test_download_verified_and_cached(self):
        result,call=self.run_source()
        self.assertEqual(result['status'],'verified'); self.assertEqual(call.call_count,2)
        self.assertEqual(result['source'],SOURCE); self.assertTrue(result['license_evidence'])
        self.assertEqual(call.call_args.args[1],'https://'+HOST+'/storage/a.pdf?download=1')
        with patch.object(self.engine,'fetch',side_effect=AssertionError('cache')):
            cached=self.engine.acquire_scihub_oa(self.meta)
            self.assertTrue(cached['cache_hit'])
            self.assertEqual(self.engine.acquire_scihub_oa(dict(self.meta,licenses=[]))['status'],'unresolved')

    def test_hash_mismatch_cache_not_reused(self):
        result,_=self.run_source()
        path=Path(result['pdf']); path.write_bytes(path.read_bytes()+b'\nmodified')
        with patch.object(self.engine,'fetch',return_value=None) as call:
            self.assertEqual(self.engine.acquire_scihub_oa(self.meta)['status'],'unresolved')
        self.assertEqual(call.call_count,1)

    def test_duplicate_links_are_one_candidate(self):
        result,call=self.run_source(b'<iframe src="/a.pdf"></iframe><a href="/a.pdf">PDF</a>')
        self.assertEqual(result['status'],'verified'); self.assertEqual(call.call_count,2)

    def test_bad_links_stop_without_pdf_request(self):
        for html,status in ((b'<iframe src="https://elsewhere.example/a.pdf">','no_pdf_link'),
                            (b'<a href="/a.pdf">a</a><a href="/b.pdf">b</a>','ambiguous_pdf_links'),
                            (b'<a href="https://[invalid">bad</a>','no_pdf_link'),
                            (b'<div class="g-recaptcha">','browser_verification_required')):
            result,call=self.run_source(html)
            self.assertEqual(result['events'][-1]['status'],status); self.assertEqual(call.call_count,1)

    def test_wrong_identity_and_not_pdf_rejected(self):
        for pdf in (b'<html>login</html>', self.pdf('10.9999/wrong')):
            result,_=self.run_source(pdf=pdf); self.assertEqual(result['status'],'unresolved')

    def test_deadline_and_disable(self):
        with patch.object(self.engine,'fetch',side_effect=AssertionError('no network')):
            self.assertEqual(self.engine.acquire_scihub_oa(self.meta,time.monotonic()-1)['events'][-1]['status'],'deadline')
            with patch.dict(os.environ,{'SCANSCI_SCIHUB_OA':'0'}):
                engine=FreeSources(self.root,config={})
                self.assertEqual(engine.acquire_scihub_oa(self.meta)['events'][-1]['status'],'disabled')

    def test_fallback_order(self):
        with patch.object(self.engine,'provider',return_value=([],[])), \
             patch.object(self.engine,'acquire_scihub_oa',return_value={'status':'unresolved','events':[]}) as call:
            self.engine.acquire(self.meta)
        self.assertEqual(call.call_count,1)
        candidate=dict(source='openalex',url='https://example.org/a.pdf',version='publishedVersion')
        with patch.object(self.engine,'provider',return_value=([candidate],[])), \
             patch.object(self.engine,'fetch',return_value=(self.pdf(),candidate['url'])), \
             patch.object(self.engine,'acquire_scihub_oa',side_effect=AssertionError('earlier success')):
            self.assertEqual(self.engine.acquire(self.meta)['status'],'verified')

    def test_crossref_retains_licenses(self):
        from pipeline import crossref
        record={'DOI':self.meta['doi'],'title':[self.meta['title']],'URL':self.meta['url'],
                'license':self.meta['licenses']}
        with patch('pipeline.remote_json',return_value={'message':record}):
            self.assertEqual(crossref(self.meta['doi'])['licenses'],self.meta['licenses'])


if __name__=='__main__': unittest.main()
