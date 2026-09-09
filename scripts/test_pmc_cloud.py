import hashlib
import json
import time
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace

from pmc_cloud import BASE, candidates, pdf_candidate, pmcid_from_url, download_pdf


class PMCTests(unittest.TestCase):
    def setUp(self):
        self.meta = {'doi': '10.1234/test', 'title': 'Example Study'}
        self.record = dict(pmcid='PMC123', version=1, doi='10.1234/test', title='Example Study',
                           is_pmc_openaccess=True, is_manuscript=False, is_retracted=False,
                           license_code='CC BY',
                           pdf_url='s3://pmc-oa-opendata/PMC123.1/PMC123.1.pdf?md5=' + hashlib.md5(b'pdf').hexdigest())
        self.listing = b'''<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
        <IsTruncated>false</IsTruncated><CommonPrefixes><Prefix>PMC123.1/</Prefix></CommonPrefixes></ListBucketResult>'''

    def test_only_real_pmc_location_paths(self):
        for u in ('https://www.ncbi.nlm.nih.gov/pmc/articles/123',
                  'https://pmc.ncbi.nlm.nih.gov/articles/PMC123/pdf/file.pdf'):
            self.assertEqual(pmcid_from_url(u), 'PMC123')
        for u in ('https://evil.example/articles/PMC123', 'https://www.ncbi.nlm.nih.gov.evil/pmc/articles/123',
                  'https://pmc.ncbi.nlm.nih.gov/articles/PMC1234junk', 'https://pubmed.ncbi.nlm.nih.gov/123/', None):
            self.assertIsNone(pmcid_from_url(u))

    def test_official_metadata_candidate_and_checksum(self):
        c = pdf_candidate(self.record, 'PMC123', 'PMC123.1/', self.meta)
        self.assertEqual(c['url'], BASE + '/PMC123.1/PMC123.1.pdf')
        self.assertEqual(c['expected_md5'], hashlib.md5(b'pdf').hexdigest())
        self.assertEqual(c['version'], 'publishedVersion')
        self.assertIn('National Library of Medicine', c['source_attribution'])

    def test_mismatched_or_nonshareable_records_rejected(self):
        for changes in ({'doi': '10.1234/other'}, {'pmcid': 'PMC456'}, {'title': 'Another Study'},
                        {'version': 2}, {'is_retracted': True}, {'is_manuscript': True},
                        {'is_pmc_openaccess': False}, {'license_code': 'TDM'}, {'license_code': 'CC BY-NC'}):
            with self.assertRaises(ValueError):
                pdf_candidate(dict(self.record, **changes), 'PMC123', 'PMC123.1/', self.meta)

    def test_pdf_origin_path_and_md5_are_not_guessed(self):
        for url in ('s3://evil/PMC123.1/PMC123.1.pdf',
                    'https://example.org/article.pdf?md5=' + 'a'*32,
                    's3://pmc-oa-opendata/PMC456.1/PMC456.1.pdf?md5=' + 'a'*32,
                    's3://pmc-oa-opendata/PMC123.1/PMC123.1.pdf',
                    's3://pmc-oa-opendata/PMC123.1/PMC123.1.pdf?md5=bad'):
            with self.assertRaises(ValueError):
                pdf_candidate(dict(self.record, pdf_url=url), 'PMC123', 'PMC123.1/', self.meta)

    def test_bounded_exact_prefix_listing_and_metadata(self):
        calls = []
        def fetch(source, url, headers, deadline, events):
            calls.append(url)
            return (self.listing if len(calls) == 1 else json.dumps(self.record).encode(), url)
        found = candidates(fetch, 'PMC123', self.meta, time.monotonic()+5, [])
        self.assertEqual(len(found), 1)
        self.assertEqual(len(calls), 2)
        self.assertIn('prefix=PMC123.', calls[0])
        self.assertIn('max-keys=10', calls[0])
        self.assertTrue(calls[1].endswith('/PMC123.1/PMC123.1.json'))

    def test_truncated_listing_is_not_silently_partial(self):
        events = []
        def fetch(*args):
            return self.listing.replace(b'false', b'true'), BASE
        self.assertEqual(candidates(fetch, 'PMC123', self.meta, time.monotonic()+5, events), [])
        self.assertEqual(events[-1]['status'], 'invalid_version_listing')

    def test_range_download_resume_and_integrity(self):
        data = b'%PDF-' + b'x' * 1048576
        url = BASE + '/PMC123.1/PMC123.1.pdf'
        candidate = {'url': url, 'expected_md5': hashlib.md5(data).hexdigest()}
        listing = ('<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                   '<Contents><Key>PMC123.1/PMC123.1.pdf</Key><Size>' + str(len(data)) +
                   '</Size><ETag>"' + 'a'*32 + '"</ETag></Contents></ListBucketResult>').encode()
        parent = Path('_work/pmc_tests'); parent.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(dir=parent))
        transfers = []
        fail_second = [True]
        def fetch(source, uri, headers, deadline, events, pdf=False):
            if not pdf:
                return listing, uri
            start, end = map(int, headers['Range'].split('=')[1].split('-'))
            self.assertEqual(headers['If-Match'], '"' + 'a'*32 + '"')
            transfers.append(start)
            if start == 524288 and fail_second[0]:
                return None
            return data[start:end+1], uri
        engine = SimpleNamespace(root=root, fetch=fetch)
        events = []
        self.assertIsNone(download_pdf(engine, candidate, time.monotonic()+5, events))
        self.assertEqual(events[-1]['completed'], 2)
        fail_second[0] = False
        transfers.clear()
        self.assertEqual(download_pdf(engine, candidate, time.monotonic()+5, [])[0], data)
        self.assertEqual(transfers, [524288])
        self.assertIsNone(download_pdf(engine, dict(candidate, expected_md5='0'*32), time.monotonic()+5, []))


if __name__ == '__main__':
    unittest.main()
