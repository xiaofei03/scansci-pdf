#!/usr/bin/env python3
"""Resumable DOI -> Zotero OA resolver -> allowlisted OA -> AbleSci queue.

No cookies, generic JS bridge, global library writes, or destructive cleanup.
The connector session route supports newly imported records; older records whose
session has expired need an explicit attachment adapter, never a duplicate item.
"""
from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
from itertools import chain
import json
import re
import sqlite3
import socket
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from pypdf import PdfReader

BASE = 'http://127.0.0.1:23119'
MAX_BYTES = 60 * 1024 * 1024


def norm(text):
    return re.sub(r'[^\w]', '', unicodedata.normalize('NFKD', text).lower())


def doi_normalize(value):
    value = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', value.strip(), flags=re.I)
    if not re.fullmatch(r'10\.\d{4,9}/\S+', value, re.I):
        raise ValueError('Invalid DOI')
    return value.lower()


def http(url, body=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=body, headers={
        'User-Agent': 'ResearchPDFPipeline/0.1 (personal research)', **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        data = res.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError('response exceeds 60 MB limit')
        return data, res.geturl(), res.status


def api(route, payload=None, timeout=30):
    raw, _, _ = http(BASE + route,
                     json.dumps(payload).encode() if payload is not None else None,
                     {'Content-Type': 'application/json'}, timeout)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.decode('utf-8')


def remote_json(url):
    return json.loads(http(url)[0])


class CitationMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'meta' and a.get('name', '').lower() in ('citation_doi', 'citation_title', 'citation_pdf_url'):
            self.values[a['name'].lower()] = a.get('content', '')


def publisher_pdf_urls(meta):
    """Read a public publisher landing page, following only its explicit PDF tag."""
    try:
        data, final, _ = http(meta['url'], timeout=20)
        parser = CitationMetadata()
        parser.feed(data.decode('utf-8', errors='replace'))
        v = parser.values
        if (v.get('citation_doi', '').lower() == meta['doi']
                and norm(v.get('citation_title', '')) == norm(meta['title'])
                and v.get('citation_pdf_url')):
            yield 'publisher_metadata', urllib.parse.urljoin(final, v['citation_pdf_url'])
    except Exception:
        return  # Authentication, robot checks, or missing metadata are not bypassed.


def crossref(doi):
    m = remote_json('https://api.crossref.org/works/' + urllib.parse.quote(doi, safe=''))['message']
    if doi_normalize(m['DOI']) != doi:
        raise ValueError('Crossref DOI mismatch')
    return dict(doi=doi, title=m['title'][0],
                authors=[a.get('family', '') for a in m.get('author', [])],
                creators=[dict(creatorType='author', firstName=a.get('given', ''), lastName=a.get('family', ''))
                          for a in m.get('author', []) if a.get('family')],
                journal=(m.get('container-title') or [''])[0],
                year=str((m.get('published', {}).get('date-parts') or [['']])[0][0]),
                url=m.get('resource', {}).get('primary', {}).get('URL') or m['URL'],
                page_range=m.get('page', ''), metadata_source='Crossref',
                links=m.get('link', []))


def validate_pdf(path, meta):
    result = dict(status='rejected', path=str(path))
    if not path.is_file() or path.stat().st_size < 1000:
        return {**result, 'reason': 'missing_or_too_small'}
    with path.open('rb') as f:
        if f.read(5) != b'%PDF-':
            return {**result, 'reason': 'not_pdf'}
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            return {**result, 'reason': 'encrypted'}
        texts = [p.extract_text() or '' for p in reader.pages]
    except Exception as e:
        return {**result, 'reason': 'unreadable_' + type(e).__name__}
    result.update(pages=len(texts), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    if len(texts) < 2 or sum(len(t.strip()) for t in texts) < 1000:
        return {**result, 'reason': 'short_or_scan_needs_review'}
    # Match the opening, never a DOI found only in the references.
    front = norm(texts[0] + texts[1][:1500])
    title_ok = norm(meta['title']) in front
    doi_tokens={d.rstrip('.,;:)]}').lower() for d in re.findall(r'10\.\d{4,9}/[^\s<>"\u201c\u201d]+',texts[0]+'\n'+texts[1][:1500],re.I)}
    doi_ok = meta['doi'].lower() in doi_tokens
    author_ok = bool(meta['authors']) and norm(meta['authors'][0]) in front
    result.update(title_match=title_ok, doi_match=doi_ok, first_author_match=author_ok)
    opening_dois={d.rstrip('.,;:)]}').lower() for d in re.findall(r'10\.\d{4,9}/[^\s<>"\u201c\u201d]+',texts[0],re.I)}
    if opening_dois and not doi_ok:
        return {**result,'status':'needs_review','reason':'opening_doi_conflicts',
                'opening_dois':sorted(opening_dois)}
    page_range = re.fullmatch(r'(\d+)\s*[-–]\s*(\d+)', meta.get('page_range', ''))
    if page_range and len(texts) < int(page_range[2]) - int(page_range[1]) + 1:
        review=meta.get('reviewed_author_version',{})
        # This is a hash-bound, explicit review outcome, never an automatic
        # inference that a short file must be a complete author manuscript.
        if (review.get('user_approved') is True and review.get('sha256')==result['sha256']
                and review.get('pages')==len(texts) and review.get('review_notes')
                and review.get('pagination_checked') is True
                and review.get('ending_checked') is True
                and title_ok and author_ok
                and all(norm(a) in front for a in meta['authors'])):
            return {**result,'status':'probably_correct','version':'author_manuscript',
                    'warning':'User-approved, hash-bound reviewed author version; not publisher typeset version',
                    'version_review':review}
        return {**result, 'reason': 'fewer_pages_than_published_range'}
    if title_ok and author_ok:
        return {**result, 'status': 'verified' if doi_ok else 'probably_correct',
                'warning': 'automated identity/readability check; not a page-by-page completeness certification'}
    return {**result, 'status': 'needs_review', 'reason': 'opening_identity_not_confirmed'}


def selected_collection(key):
    expected = api('/api/users/0/collections/' + key)['data']
    selected = api('/connector/getSelectedCollection', {})
    if selected.get('libraryID') != 1 or selected.get('id') is None:
        raise RuntimeError('Select target collection in personal Zotero library')
    if selected.get('name') != expected['name'] or not selected.get('filesEditable'):
        raise RuntimeError('Selected collection differs from requested editable target')
    # Duplicate collection names are ambiguous: never infer identity from name alone.
    matches = [c for c in api('/api/users/0/collections?limit=100') if c['data']['name'] == expected['name']]
    if len(matches) != 1 or matches[0]['key'] != key:
        raise RuntimeError('Collection-name ambiguity; explicit identity adapter required')
    return selected


def existing(meta):
    rows = []
    for query in (meta['doi'], meta['title']):
        rows += api('/api/users/0/items/top?limit=100&q=' + urllib.parse.quote(query))
    return {r['key']: r for r in rows if
            r['data'].get('DOI', '').lower() == meta['doi'] or norm(r['data'].get('title', '')) == norm(meta['title'])}


def managed_pdfs(key, meta):
    found = []
    for a in api('/api/users/0/items/' + key + '/children?limit=100'):
        if a['data'].get('contentType') != 'application/pdf':
            continue
        try:
            raw, _, _ = http(BASE + '/api/users/0/items/' + a['key'] + '/file/view/url')
            value = raw.decode().strip()
            if value.startswith('"'):
                value = json.loads(value)
            path = Path(urllib.parse.unquote(urllib.parse.urlparse(value).path))
            checked = validate_pdf(path, meta)
            checked.update(attachment_key=a['key'], link_mode=a['data'].get('linkMode'))
            # Only Zotero-managed files count as finished.
            checked['managed'] = a['data'].get('linkMode') in ('imported_file', 'imported_url')
            found.append(checked)
        except Exception as e:
            found.append(dict(status='rejected', reason=type(e).__name__, attachment_key=a['key']))
    return found


def good(attachments):
    return any(a.get('managed') and a['status'] in ('verified', 'probably_correct') for a in attachments)


def safe_resolver_prefs(prefs):
    text = Path(prefs).read_text(encoding='utf-8')
    m = re.search(r'user_pref\("extensions\.zotero\.findPDFs\.resolvers", (.*)\);', text)
    if m:
        resolvers = json.loads(json.loads(m[1]))
        if isinstance(resolvers, dict):
            resolvers = [resolvers]
        if any(r.get('automatic') for r in resolvers):
            raise RuntimeError('Automatic custom resolvers exist; review before using connector OA route')


class Batch:
    def __init__(self, directory, collection, prefs, zotero_oa=True):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.collection, self.prefs = collection, prefs
        self.zotero_oa = zotero_oa
        self.db = sqlite3.connect(self.root / 'jobs.sqlite')
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (doi TEXT PRIMARY KEY, data TEXT NOT NULL)')

    def save(self, job):
        self.db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?)', (job['doi'], json.dumps(job, ensure_ascii=False)))
        self.db.commit()

    def event(self, job, stage, **data):
        row = dict(at=time.time(), doi=job['doi'], stage=stage, **data)
        with (self.root / 'events.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
        print(json.dumps(row, ensure_ascii=False), flush=True)

    def run_one(self, doi, metadata_only=False):
        row = self.db.execute('SELECT data FROM jobs WHERE doi=?', (doi,)).fetchone()
        job = json.loads(row[0]) if row else dict(doi=doi, status='new')
        if not job.get('metadata'):
            job['metadata'] = crossref(doi)
        meta = job['metadata']
        self.save(job)
        matches = existing(meta)
        if len(matches) > 1:
            job['status'] = 'duplicate_needs_review'
            self.save(job)
            return job
        if matches:
            key, record = next(iter(matches.items()))
            if self.collection not in record['data'].get('collections', []):
                job.update(status='existing_outside_target', key=key)
                self.save(job)
                return job
            job['key'] = key
            job.pop('metadata_write_pending',None)
            attachments = managed_pdfs(key, meta)
            if good(attachments):
                job.update(status='complete', attachments=attachments)
                self.save(job)
                self.event(job, 'verified_existing')
                return job
        else:
            if job.get('metadata_write_pending'):
                raise RuntimeError('Earlier metadata write unresolved; reconcile before another saveItems')
            selected_collection(self.collection)
            job.update(session=str(uuid.uuid4()), connector_id=str(uuid.uuid4()), status='importing_metadata')
            job['metadata_write_pending']=True
            self.save(job)
            api('/connector/saveItems', dict(sessionID=job['session'], uri=meta['url'], items=[dict(
                id=job['connector_id'], itemType='journalArticle', title=meta['title'], DOI=doi,
                creators=meta['creators'], date=meta['year'], publicationTitle=meta['journal'],
                url=meta['url'], tags=[], attachments=[])]))
            matches = existing(meta)
            if len(matches) != 1:
                raise RuntimeError('Imported item identity not unique')
            job['key'] = next(iter(matches))
            job.pop('metadata_write_pending',None)
            selected_collection(self.collection)
            self.save(job)
        if metadata_only:
            job['status'] = 'metadata_ready'
            self.save(job)
            return job
        if job.get('resolver_pending'):
            job['status'] = 'resolver_pending'
            self.save(job)
            return job
        if self.zotero_oa and job.get('session') and not job.get('resolver_attempted'):
            safe_resolver_prefs(self.prefs)
            self.event(job, 'zotero_oa_resolver_started')
            job['resolver_attempted'] = True
            self.save(job)
            try:
                api('/connector/saveAttachmentFromResolver', dict(sessionID=job['session'], itemID=job['connector_id']), timeout=55)
            except Exception as e:
                self.event(job, 'zotero_oa_resolver_returned', result=type(e).__name__)
                if isinstance(e, (TimeoutError, socket.timeout)) or (isinstance(e, urllib.error.URLError) and isinstance(e.reason, (TimeoutError, socket.timeout))):
                    job['resolver_pending'] = True
                    self.save(job)
            attachments = managed_pdfs(job['key'], meta)
            if good(attachments):
                job.update(status='complete', source='zotero_oa', attachments=attachments)
                self.save(job)
                self.event(job, 'complete', source='zotero_oa')
                return job
            # A timed-out server call might still be running. Do not race it with writes.
            if job.get('resolver_pending'):
                job['status'] = 'resolver_pending'
                self.save(job)
                return job
        candidates = []
        try:
            oa = remote_json('https://api.openalex.org/works/https://doi.org/' + urllib.parse.quote(doi, safe=''))
            if (oa.get('doi') or '').lower().removeprefix('https://doi.org/') == doi:
                candidates += [('openalex', loc['pdf_url']) for loc in oa.get('locations', [])
                               if loc.get('is_oa') and loc.get('pdf_url')]
        except Exception as e:
            self.event(job, 'openalex', result=type(e).__name__)
        # Crossref publisher-provided PDF URLs are attempted normally, never bypassed.
        candidates += [('publisher', l['URL']) for l in meta['links'] if l.get('content-type') == 'application/pdf']
        seen = set()
        for source, url in chain(candidates, publisher_pdf_urls(meta)):
            if url in seen:
                continue
            if len(seen) >= 6:
                break
            seen.add(url)
            path = self.root / (hashlib.sha256((doi + url).encode()).hexdigest()[:20] + '.pdf')
            try:
                if not path.exists():
                    data, final, _ = http(url, timeout=25)
                    if not data.startswith(b'%PDF-'):
                        self.event(job, source, result='not_pdf')
                        continue
                    path.write_bytes(data)
                check = validate_pdf(path, meta)
                self.event(job, source, validation=check)
                if check['status'] not in ('verified', 'probably_correct'):
                    continue
                job.update(pdf=str(path), source=source, source_url=url, validation=check)
                self.save(job)
                return self.attach(job)
            except Exception as e:
                self.event(job, source, result=type(e).__name__)
            time.sleep(1)
        job['status'] = 'needs_ablesci'
        self.save(job)
        self.event(job, 'needs_ablesci')
        return job

    def attach(self, job):
        if good(managed_pdfs(job['key'], job['metadata'])):
            job['status'] = 'complete'
        elif not job.get('session'):
            job['status'] = 'attachment_session_required'
        else:
            selected_collection(self.collection)
            metadata = dict(sessionID=job['session'], parentItemID=job['connector_id'],
                            title='Full Text PDF', url=job['source_url'])
            try:
                raw, _, status = http(BASE + '/connector/saveAttachment', Path(job['pdf']).read_bytes(),
                    {'Content-Type': 'application/pdf', 'X-Metadata': json.dumps(metadata)}, 45)
                checks = managed_pdfs(job['key'], job['metadata'])
                job.update(status='complete' if good(checks) else 'attachment_verification_failed', attachments=checks)
            except Exception as e:
                job.update(status='attachment_session_required', attachment_error=type(e).__name__)
        self.save(job)
        self.event(job, job['status'])
        return job

    def report(self):
        rows = [json.loads(r[0]) for r in self.db.execute('SELECT data FROM jobs ORDER BY doi')]
        (self.root / 'summary.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
        return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--collection', required=True)
    parser.add_argument('--work-dir', required=True)
    parser.add_argument('--prefs', required=True)
    parser.add_argument('--doi', action='append', default=[])
    parser.add_argument('--doi-file', help='UTF-8, one DOI per line; blank/comment lines ignored')
    parser.add_argument('--test-skip-zotero-oa', action='store_true', help='Isolated fallback test only; recorded in event log')
    parser.add_argument('--metadata-only', action='store_true')
    args = parser.parse_args()
    batch = Batch(args.work_dir, args.collection, args.prefs, not args.test_skip_zotero_oa)
    if args.test_skip_zotero_oa:
        batch.event({'doi': ''}, 'test_mode', zotero_oa='intentionally_skipped')
    inputs = list(args.doi)
    if args.doi_file:
        inputs += [line.strip() for line in Path(args.doi_file).read_text(encoding='utf-8').splitlines()
                   if line.strip() and not line.lstrip().startswith('#')]
    for doi in dict.fromkeys(doi_normalize(d) for d in inputs):
        try:
            result = batch.run_one(doi, metadata_only=args.metadata_only)
            print(json.dumps({'doi': doi, 'status': result['status']}, ensure_ascii=False), flush=True)
        except Exception as e:
            row = batch.db.execute('SELECT data FROM jobs WHERE doi=?', (doi,)).fetchone()
            failed = json.loads(row[0]) if row else dict(doi=doi)
            failed.update(status='error', error_type=type(e).__name__)
            if isinstance(e, urllib.error.HTTPError):
                failed['http_status'] = e.code
            batch.save(failed)
            batch.event(failed, 'error', error_type=type(e).__name__)
            print(json.dumps({'doi': doi, 'error': str(e)}, ensure_ascii=False), flush=True)
        finally:
            batch.report()


if __name__ == '__main__':
    main()
