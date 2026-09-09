import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from reportlab.pdfgen import canvas
from free_sources import FreeSources, SafeRedirect, public_url, settings


class FreeTests(unittest.TestCase):
    def setUp(self):
        root = Path('_work/free_source_tests'); root.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=root))
        self.engine = FreeSources(self.root, config={})
        self.meta = dict(doi='10.1234/test', title='Research on organizations', authors=['Smith'],
                         page_range='1-2', url='https://publisher.example/article', links=[])

    def pdf(self):
        out = io.BytesIO(); c = canvas.Canvas(out)
        for _ in range(2):
            c.drawString(20, 800, 'Research on organizations 10.1234/test Smith')
            for y in range(40, 750, 20): c.drawString(20, y, 'Results methods discussion reference evidence. '*2)
            c.showPage()
        c.save(); return out.getvalue()

    def test_config_redacted_status_input(self):
        path = self.root/'config.json'; path.write_text(json.dumps({'email':'a@b.org','openalex_api_key':'local'}))
        with patch.dict(os.environ, {'OPENALEX_API_KEY':'env'}):
            self.assertEqual(settings(path)['openalex_api_key'], 'env')

    def test_missing_email_skips_unpaywall(self):
        with patch.object(self.engine, 'fetch', side_effect=AssertionError('network forbidden')):
            candidates, events = self.engine.provider('unpaywall', self.meta, time.monotonic()+3)
        self.assertEqual(candidates, []); self.assertEqual(events[0]['status'],'not_configured')

    def test_unpaywall_identity(self):
        self.engine.config['email']='a@b.org'
        record = {'doi':'10.1234/wrong','oa_locations':[{'url_for_pdf':'https://example.org/x.pdf'}]}
        with patch.object(self.engine,'fetch',return_value=(json.dumps(record).encode(),'https://api.unpaywall.org')):
            candidates, events = self.engine.provider('unpaywall', self.meta,time.monotonic()+5)
        self.assertFalse(candidates); self.assertEqual(events[-1]['status'],'invalid_metadata_or_identity')

    def test_openalex_only_oa_and_bearer(self):
        self.engine.config['openalex_api_key']='SECRET'
        record = {'doi':self.meta['doi'],'locations':[{'is_oa':False,'pdf_url':'https://example.org/no.pdf'},
                  {'is_oa':True,'pdf_url':'https://example.org/yes.pdf','version':'acceptedVersion'}]}
        with patch.object(self.engine,'fetch',return_value=(json.dumps(record).encode(),'https://api.openalex.org')) as fetch:
            candidates, _ = self.engine.provider('openalex',self.meta,time.monotonic()+5)
        self.assertEqual(len(candidates),1)
        self.assertNotIn('SECRET',fetch.call_args.args[1])
        self.assertEqual(fetch.call_args.args[2]['Authorization'],'Bearer SECRET')

    def test_http_error_is_classified_without_secret(self):
        events=[]
        err=urllib.error.HTTPError('https://api.example/?api_key=SECRET',401,'SECRET',{},None)
        with patch('free_sources.download_bytes',side_effect=err):
            self.engine.fetch('openalex',err.url,{},time.monotonic()+2,events)
        self.assertEqual(events[0]['status'],'authentication_required')
        self.assertNotIn('SECRET',json.dumps(events))

    def test_rate_limit_cooldown_no_repeat(self):
        events=[]; err=urllib.error.HTTPError('https://api.example/',429,'limited',{'Retry-After':'200'},None)
        with patch('free_sources.download_bytes',side_effect=err) as call:
            for _ in range(2): self.engine.fetch('test',err.url,{},time.monotonic()+2,events)
        self.assertEqual(call.call_count,1); self.assertEqual(events[-1]['status'],'host_cooldown')

    def test_no_private_or_credential_urls(self):
        for url in ('file:///a','http://127.0.0.1/a','http://localhost/a','http://u:p@example.org/a','http://[::1]/a'):
            with self.assertRaises(ValueError): public_url(url)

    def test_cross_origin_redirect_strips_keys(self):
        req=urllib.request.Request('https://api.elsevier.com/a',headers={'X-ELS-APIKey':'secret','X-ELS-Insttoken':'token','Authorization':'Bearer key'})
        out=SafeRedirect().redirect_request(req,None,302,'',{},'https://cdn.example/a')
        self.assertFalse(any(x.lower() in ('authorization','x-els-apikey','x-els-insttoken') for x in out.headers))
        with self.assertRaises(ValueError):
            SafeRedirect().redirect_request(req,None,302,'',{},'https://cdn.example/a?api_key=secret')

    def test_pdf_dedupe_and_verified_cache(self):
        candidate=dict(source='unpaywall',url='https://example.org/a.pdf#fragment',version='publishedVersion')
        with patch.object(self.engine,'provider',return_value=([candidate],[])), \
             patch.object(self.engine,'fetch',return_value=(self.pdf(),'https://example.org/a.pdf?token=secret')) as call:
            result=self.engine.acquire(self.meta)
        self.assertEqual(result['status'],'verified'); self.assertEqual(call.call_count,1)
        self.assertEqual(result['source_url'],'https://example.org/a.pdf')
        with patch.object(self.engine,'provider',side_effect=AssertionError('cache should avoid network')):
            self.assertEqual(self.engine.acquire(self.meta)['events'][0]['source'],'cache')

    def test_duplicate_bad_url_attempted_once(self):
        c=dict(source='openalex',url='https://example.org/a.pdf',version='unknown')
        with patch.object(self.engine,'provider',return_value=([c],[])), \
             patch.object(self.engine,'fetch',return_value=(b'<html>Login</html>',c['url'])) as call:
            result=self.engine.acquire(self.meta)
        self.assertEqual(result['status'],'unresolved'); self.assertEqual(call.call_count,1)
        self.assertTrue(any(e['status']=='not_pdf' for e in result['events']))

    def test_deadline_skips_pdf(self):
        self.engine.seconds=0
        with patch.object(self.engine,'provider',return_value=([dict(source='publisher',url='https://example.org/a',version='unknown')],[])), \
             patch.object(self.engine,'fetch',side_effect=AssertionError('past deadline')):
            result=self.engine.acquire(self.meta)
        self.assertTrue(result['deadline_reached'])

    def test_elsevier_only_matching_publisher_and_key(self):
        self.engine.config['elsevier_api_key']='secret'
        with patch.object(self.engine,'provider',return_value=([],[])), \
             patch.object(self.engine,'fetch',return_value=None) as call:
            self.engine.acquire(self.meta); self.assertEqual(call.call_count,0)
            self.engine.acquire(dict(self.meta,publisher='Elsevier BV'))
        self.assertEqual(call.call_count,1)
        self.assertEqual(call.call_args.args[2]['X-ELS-APIKey'],'secret')
        self.assertNotIn('view=',call.call_args.args[1])

    def test_elsevier_xml_error_redacted(self):
        events=[]; self.engine.config['elsevier_api_key']='SECRET'
        xml=b'<service-error><status><statusCode>INVALID_INPUT</statusCode><statusText>Bad SECRET https://example.org/key</statusText></status></service-error>'
        err=urllib.error.HTTPError('https://api.elsevier.com/a',400,'bad',{},io.BytesIO(xml))
        with patch('free_sources.download_bytes',side_effect=err):
            self.engine.fetch('elsevier_api',err.url,{},time.monotonic()+3,events)
        self.assertEqual(events[0]['provider_error_code'],'INVALID_INPUT')
        self.assertNotIn('SECRET',json.dumps(events))
        self.assertNotIn('https://example.org/key',json.dumps(events))

    def test_discovery_concurrent(self):
        import threading
        barrier=threading.Barrier(2)
        def provider(source, *args):
            if source != 'publisher_metadata': barrier.wait(timeout=2)
            return [],[]
        with patch.object(self.engine,'provider',side_effect=provider):
            self.assertEqual(self.engine.acquire(self.meta)['status'],'unresolved')

    def test_success_does_not_fetch_publisher_html(self):
        calls=[]
        def provider(source,*args):
            calls.append(source)
            self.assertNotEqual(source,'publisher_metadata')
            return [dict(source=source,url='https://example.org/x.pdf',version='publishedVersion')],[]
        with patch.object(self.engine,'provider',side_effect=provider), \
             patch.object(self.engine,'fetch',return_value=(self.pdf(),'https://example.org/x.pdf')):
            self.assertEqual(self.engine.acquire(self.meta)['status'],'verified')
        self.assertEqual(sorted(calls),['openalex','unpaywall'])


if __name__ == '__main__': unittest.main()
