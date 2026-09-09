import io
import json
import os
import ssl
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from reportlab.pdfgen import canvas
from free_sources import FreeSources, SafeRedirect, public_url, settings, download_bytes


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

    def test_pmc_landing_page_is_preserved_without_pdf_url(self):
        record = {'doi': self.meta['doi'], 'locations': [
            {'is_oa': True, 'pdf_url': None, 'landing_page_url': 'https://www.ncbi.nlm.nih.gov/pmc/articles/123'}]}
        with patch.object(self.engine, 'fetch', return_value=(json.dumps(record).encode(), 'https://api.openalex.org')):
            found, _ = self.engine.provider('openalex', self.meta, time.monotonic()+5)
        self.assertEqual(found, [{'source': 'pmc_cloud', 'pmcid': 'PMC123', 'version': 'unknown'}])

    def test_explicit_crossref_pdf_with_unspecified_mime(self):
        self.meta['links'] = [{'URL': 'https://publisher.example/article/pdf', 'content-type': 'unspecified'}]
        with patch.object(self.engine, 'provider', return_value=([], [])), \
             patch.object(self.engine, 'fetch', return_value=(self.pdf(), self.meta['links'][0]['URL'])) as fetch:
            self.assertEqual(self.engine.acquire(self.meta)['status'], 'verified')
        self.assertEqual(fetch.call_args.args[1], self.meta['links'][0]['URL'])

    def test_range_response_must_be_partial_and_match_requested_bytes(self):
        from free_sources import _download_bytes
        from unittest.mock import MagicMock
        for status, content_range in ((200, 'bytes 0-3/8'), (206, 'bytes 4-7/8'), (206, '')):
            response = MagicMock()
            response.status, response.headers = status, {'Content-Range': content_range}
            response.__enter__.return_value = response
            with patch('free_sources.request.build_opener') as opener:
                opener.return_value.open.return_value = response
                with self.assertRaisesRegex(ValueError, 'invalid_partial_response'):
                    _download_bytes('https://pmc-oa-opendata.s3.amazonaws.com/file', {'Range': 'bytes=0-3'},
                                    time.monotonic()+5, 100, 2)
            response.read1.assert_not_called()

    def test_pmc_checksum_failure_does_not_accept_pdf(self):
        candidate = dict(source='pmc_cloud', pmcid='PMC123', version='unknown')
        resolved = dict(source='pmc_cloud', url='https://pmc-oa-opendata.s3.amazonaws.com/PMC123.1/PMC123.1.pdf',
                        version='publishedVersion', expected_md5='0'*32)
        with patch.object(self.engine, 'provider', side_effect=lambda source, *args: ([], []) if source=='publisher_metadata' else ([candidate], [])), \
             patch('pmc_cloud.candidates', return_value=[resolved]), \
             patch('pmc_cloud.download_pdf', return_value=(self.pdf(), resolved['url'])):
            result = self.engine.acquire(self.meta)
        self.assertEqual(result['status'], 'unresolved')
        self.assertTrue(any(e['status']=='checksum_mismatch' for e in result['events']))

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

    def test_tls_record_retry_keeps_verification_and_deadline(self):
        failure=ssl.SSLError(1, '[SSL] record layer failure')
        events=[]; deadline=time.monotonic()+3
        with patch('free_sources._download_bytes',side_effect=[failure,(b'%PDF-ok','https://example.org/a')]) as call:
            result=download_bytes('https://example.org/a',{},deadline,100,transport_events=events)
        self.assertEqual(result[0],b'%PDF-ok'); self.assertEqual(call.call_count,2)
        ctx=call.call_args.kwargs['context']
        self.assertEqual(ctx.verify_mode,ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        self.assertEqual(ctx.minimum_version,ssl.TLSVersion.TLSv1_2)
        self.assertEqual(ctx.maximum_version,ssl.TLSVersion.TLSv1_2)
        self.assertEqual(call.call_args.args[2],deadline)
        self.assertEqual(call.call_args.args[3],100)
        self.assertEqual(events,[dict(status='tls12_retry',reason='tls_record_error')])

    def test_tls_certificate_error_never_retries(self):
        # Even misleading wording cannot turn a verification error into a retry.
        failure=ssl.SSLCertVerificationError(1,'record layer failure')
        with patch('free_sources._download_bytes',side_effect=failure) as call:
            with self.assertRaises(ssl.SSLCertVerificationError):
                download_bytes('https://example.org/a',{},time.monotonic()+3,100)
        self.assertEqual(call.call_count,1)

    def test_tls_retry_once_only(self):
        failure=ssl.SSLError(1,'[SSL] record layer failure')
        with patch('free_sources._download_bytes',side_effect=failure) as call:
            with self.assertRaises(ssl.SSLError):
                download_bytes('https://example.org/a',{},time.monotonic()+3,100)
        self.assertEqual(call.call_count,2)

    def test_tls_generic_failure_never_changes_protocol(self):
        failure=urllib.error.URLError(ssl.SSLError(1,'handshake failure'))
        with patch('free_sources._download_bytes',side_effect=failure) as call:
            with self.assertRaises(urllib.error.URLError):
                download_bytes('https://example.org/a',{},time.monotonic()+3,100)
        self.assertEqual(call.call_count,1)

    def test_tls_wrapped_record_failure_can_retry(self):
        failure=urllib.error.URLError(ssl.SSLError(1,'record layer failure'))
        with patch('free_sources._download_bytes',side_effect=[failure,(b'ok','https://example.org')]) as call:
            download_bytes('https://example.org/a',{},time.monotonic()+3,100)
        self.assertEqual(call.call_count,2)

    def test_tls_expired_deadline_no_retry(self):
        failure=ssl.SSLError(1,'record layer failure')
        with patch('free_sources._download_bytes',side_effect=failure) as call:
            with self.assertRaises(ssl.SSLError): download_bytes('https://example.org/a',{},0,100)
        self.assertEqual(call.call_count,1)

    def test_cf_challenge_stops_same_host_not_other_sources(self):
        events=[]
        failure=urllib.error.HTTPError('https://publisher.example/a',403,'Forbidden',{'cf-mitigated':'challenge'},None)
        with patch('free_sources.download_bytes',side_effect=[failure,(b'other','https://repository.example')]) as call:
            self.engine.fetch('publisher',failure.url,{},time.monotonic()+3,events)
            self.engine.fetch('publisher','https://publisher.example/b',{},time.monotonic()+3,events)
            result=self.engine.fetch('repository','https://repository.example',{},time.monotonic()+3,events)
        self.assertEqual(call.call_count,2)
        self.assertEqual(events[0]['status'],'browser_verification_required')
        self.assertEqual(events[1]['status'],'challenge_host_skipped')
        self.assertEqual(result[0],b'other')

    def test_plain_403_is_not_assumed_cloudflare(self):
        events=[]
        failure=urllib.error.HTTPError('https://publisher.example/a',403,'Forbidden',{},None)
        with patch('free_sources.download_bytes',side_effect=failure):
            self.engine.fetch('publisher',failure.url,{},time.monotonic()+3,events)
        self.assertEqual(events[0]['status'],'access_denied')
        self.assertFalse(self.engine.challenge_hosts)

    def test_tls_failure_and_retry_are_observable(self):
        events=[]
        failure=ssl.SSLError(1,'record layer failure')
        with patch('free_sources._download_bytes',side_effect=failure):
            self.engine.fetch('repository','https://repository.example/a',{},time.monotonic()+3,events)
        self.assertEqual(events[0]['status'],'tls_record_error')
        self.assertEqual(events[-1]['status'],'tls12_retry')

    def test_certificate_failure_has_specific_diagnostic(self):
        events=[]
        failure=ssl.SSLCertVerificationError(1,'certificate verify failed')
        failure.verify_code=20
        with patch('free_sources.download_bytes',side_effect=urllib.error.URLError(failure)):
            self.engine.fetch('repository','https://repository.example/a',{},time.monotonic()+3,events)
        self.assertEqual(events[0]['status'],'tls_certificate_error')
        self.assertEqual(events[0]['certificate_verify_code'],20)


if __name__ == '__main__': unittest.main()
