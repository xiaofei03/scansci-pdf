"""Resolve indexed PMC locations using NLM's public, anonymous S3 dataset.

Protocol: https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/ (checked 2026-09-09).
No PMC webpage scraping, legacy OA API, AWS account or corpus-wide listing.
"""
import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
import re
import time
import urllib.parse as urlparse
import xml.etree.ElementTree as ET

BUCKET = 'pmc-oa-opendata'
BASE = 'https://' + BUCKET + '.s3.amazonaws.com'
NS = {'s': 'http://s3.amazonaws.com/doc/2006-03-01/'}


def pmcid_from_url(url):
    if not isinstance(url, str):
        return None
    p = urlparse.urlsplit(url)
    if p.scheme not in ('http', 'https') or p.username or p.password:
        return None
    if p.hostname == 'pmc.ncbi.nlm.nih.gov':
        match = re.fullmatch(r'/articles/(?:PMC)?([1-9][0-9]{0,12})(?:/.*)?', p.path)
    elif p.hostname in ('www.ncbi.nlm.nih.gov', 'ncbi.nlm.nih.gov'):
        match = re.fullmatch(r'/pmc/articles/(?:PMC)?([1-9][0-9]{0,12})(?:/.*)?', p.path)
    else:
        return None
    return 'PMC' + match[1] if match else None


def pdf_candidate(record, pmcid, prefix, meta):
    from pipeline import norm, doi_normalize
    if (record.get('pmcid') != pmcid or str(record.get('version')) != prefix.rstrip('/').split('.')[-1]
            or doi_normalize(record.get('doi', '')) != meta['doi']
            or norm(record.get('title', '')) != norm(meta['title'])):
        raise ValueError('pmc_identity_mismatch')
    # Do not substitute author manuscripts/retractions or infer rights from an OA flag.
    if (record.get('is_pmc_openaccess') is not True or record.get('is_manuscript') is not False
            or record.get('is_retracted') is not False
            or record.get('license_code') not in ('CC BY', 'CC BY-SA', 'CC0')):
        raise ValueError('pmc_rights_or_version_not_supported')
    p = urlparse.urlsplit(record.get('pdf_url') or '')
    if (p.scheme, p.netloc) not in (('s3', BUCKET), ('https', BUCKET + '.s3.amazonaws.com')):
        raise ValueError('pmc_pdf_missing_or_external')
    if p.path != '/' + prefix + prefix.rstrip('/') + '.pdf' or p.fragment:
        raise ValueError('pmc_pdf_key_mismatch')
    query = urlparse.parse_qs(p.query)
    md5 = query.get('md5', [])
    if set(query) != {'md5'} or len(md5) != 1 or not re.fullmatch(r'[a-fA-F0-9]{32}', md5[0]):
        raise ValueError('pmc_checksum_missing')
    return dict(source='pmc_cloud', url=BASE + p.path, version='publishedVersion',
                expected_md5=md5[0].lower(), repository_id=prefix.rstrip('/'),
                source_attribution='Source: NIH National Library of Medicine, PMC Article Datasets on AWS.')


def candidates(fetch, pmcid, meta, deadline, events):
    if not re.fullmatch(r'PMC[1-9][0-9]{0,12}', pmcid):
        return []
    query = urlparse.urlencode({'list-type': 2, 'prefix': pmcid + '.', 'delimiter': '/', 'max-keys': 10})
    result = fetch('pmc_cloud', BASE + '/?' + query, {}, deadline, events)
    if not result:
        return []
    try:
        root = ET.fromstring(result[0])
        if root.findtext('s:IsTruncated', namespaces=NS) != 'false':
            raise ValueError('pmc_version_listing_truncated')
        prefixes = [e.text for e in root.findall('s:CommonPrefixes/s:Prefix', NS)]
        if len(prefixes) > 3 or any(not re.fullmatch(re.escape(pmcid) + r'\.[1-9][0-9]*/', p or '') for p in prefixes):
            raise ValueError('pmc_version_listing_needs_review')
    except (ET.ParseError, ValueError):
        events.append(dict(source='pmc_cloud', status='invalid_version_listing'))
        return []
    found = []
    for prefix in prefixes:
        if time.monotonic() >= deadline:
            break
        result = fetch('pmc_cloud', BASE + '/' + prefix + prefix.rstrip('/') + '.json', {}, deadline, events)
        if not result:
            continue
        try:
            found.append(pdf_candidate(json.loads(result[0]), pmcid, prefix, meta))
        except (ValueError, TypeError, AttributeError):
            events.append(dict(source='pmc_cloud', status='metadata_rights_or_identity_rejected', pmcid=pmcid))
    # Do not assume the numerically latest deposit is the desired published version.
    if len(found) > 1:
        events.append(dict(source='pmc_cloud', status='ambiguous_published_versions', pmcid=pmcid))
        return []
    events.append(dict(source='pmc_cloud', status='candidates', count=len(found)))
    return found


def download_pdf(engine, candidate, deadline, events):
    """Two bounded range workers, resumable complete chunks, final whole-file MD5.

    List only the declared PDF object. Each request uses S3 If-Match and strict
    Content-Range validation in the shared transport; changed objects never mix.
    """
    url = candidate['url']
    if not url.startswith(BASE + '/'):
        return None
    key = urlparse.urlsplit(url).path.lstrip('/')
    listing = BASE + '/?' + urlparse.urlencode({'list-type': 2, 'prefix': key, 'max-keys': 2})
    result = engine.fetch('pmc_cloud', listing, {}, deadline, events)
    if not result:
        return None
    try:
        tree = ET.fromstring(result[0])
        objects = [x for x in tree.findall('s:Contents', NS) if x.findtext('s:Key', namespaces=NS) == key]
        if len(objects) != 1:
            raise ValueError()
        size = int(objects[0].findtext('s:Size', namespaces=NS))
        etag = objects[0].findtext('s:ETag', namespaces=NS)
        if not (0 < size <= 60*1024*1024) or not re.fullmatch(r'"[a-fA-F0-9]{32}(?:-\d+)?"', etag or ''):
            raise ValueError()
    except (ET.ParseError, ValueError, TypeError):
        events.append(dict(source='pmc_cloud', status='object_listing_rejected'))
        return None
    folder = engine.root / 'pmc_parts' / hashlib.sha256((url + etag).encode()).hexdigest()[:24]
    folder.mkdir(parents=True, exist_ok=True)
    chunk_size = 512*1024
    tasks = [(n, start, min(start+chunk_size, size)-1) for n, start in enumerate(range(0, size, chunk_size))]

    def part(task):
        n, start, end = task
        path = folder / (str(n) + '.part')
        info = folder / (str(n) + '.sha256')
        try:
            data = path.read_bytes()
            if len(data) == end-start+1 and hashlib.sha256(data).hexdigest() == info.read_text(encoding='ascii'):
                return data
        except OSError:
            pass
        if time.monotonic() >= deadline:
            return None
        found = engine.fetch('pmc_cloud_range', url, {'Accept': 'application/pdf',
                             'Range': f'bytes={start}-{end}', 'If-Match': etag}, deadline, events, pdf=True)
        if not found or len(found[0]) != end-start+1:
            return None
        path.write_bytes(found[0])
        info.write_text(hashlib.sha256(found[0]).hexdigest(), encoding='ascii')
        return found[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        parts = list(pool.map(part, tasks))
    complete = sum(p is not None for p in parts)
    events.append(dict(source='pmc_cloud', status='parts_checkpoint', completed=complete, total=len(tasks),
                       bytes=sum(len(p) for p in parts if p is not None)))
    if complete != len(parts):
        return None
    data = b''.join(parts)
    if hashlib.md5(data).hexdigest() != candidate['expected_md5']:
        events.append(dict(source='pmc_cloud', status='checksum_mismatch'))
        return None
    return data, url
