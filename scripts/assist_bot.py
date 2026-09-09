#!/usr/bin/env python3
"""Headless AbleSci helping client. No browser, cookies on disk, or Zotero writes.

Upload handshake observed in the site's SimpleUpload form on 2026-09-09.
Unknown writes never auto-retry. Run scan first; live mode requires explicit flags.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import hashlib
from html.parser import HTMLParser
import http.cookiejar
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse as urlparse
import urllib.request as request
import uuid

BASE = 'https://www.ablesci.com'
STOP = threading.Event()
VOID = set('area base br col embed hr img input link meta param source track wbr'.split())


class Halt(RuntimeError):
    """An authentication, schema or uncertain-write condition requiring review."""


class Node:
    def __init__(self, tag='', attrs=(), parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []

    def all(self, tag=None, cls=None):
        for child in self.children:
            if isinstance(child, Node):
                if (tag is None or child.tag == tag) and (cls is None or cls in child.attrs.get('class', '').split()):
                    yield child
                yield from child.all(tag, cls)

    def text(self):
        return ' '.join(c.text() if isinstance(c, Node) else c for c in self.children
                        if not isinstance(c, Node) or c.tag not in ('script', 'style'))


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = self.current = Node()
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs, self.current)
        self.current.children.append(n)
        if tag not in VOID:
            self.current = n

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        p = self.current
        while p.parent is not None:
            if p.tag == tag:
                self.current = p.parent
                break
            p = p.parent

    def handle_data(self, data):
        self.current.children.append(data)


def one(nodes):
    values = list(nodes)
    if len(values) != 1:
        raise Halt('page_schema_changed')
    return values[0]


def request_id(href):
    p = urlparse.urlsplit(urlparse.urljoin(BASE, href))
    ids = urlparse.parse_qs(p.query).get('id', [])
    if p.scheme != 'https' or p.netloc != 'www.ablesci.com' or p.path != '/assist/detail':
        return None
    return ids[0] if len(ids) == 1 and re.fullmatch(r'[A-Za-z0-9]{4,20}', ids[0]) else None


def scan_page(html):
    root = Page(html).root
    rows = []
    for heading in root.all('h2', 'assist-list-title'):
        # Skip pinned notices and files already uploaded, even on the waiting list.
        link = next((a for a in heading.all('a') if request_id(a.attrs.get('href', ''))), None)
        if link is None or any(w in link.attrs.get('class', '') for w in ('stick-assist', 'uploaded', 'completed')):
            continue
        rows.append({'id': request_id(link.attrs['href']), 'title': ' '.join(link.text().split())})
    return list({r['id']: r for r in rows}.values())


def detail_page(html, expected_id):
    from pipeline import doi_normalize
    root = Page(html).root
    badge = one(root.all('span', 'assist-badge'))
    state = 'waiting' if 'assist-badge-waiting' in badge.attrs.get('class', '').split() else 'closed_or_uploaded'
    identity = one(root.all('input', 'assist-id-val')).attrs.get('value')
    if identity != expected_id:
        raise Halt('request_id_mismatch')
    cells = {}
    for table in root.all('table', 'stable'):
        for tr in table.all('tr'):
            td = [c for c in tr.children if isinstance(c, Node) and c.tag == 'td']
            if len(td) == 2:
                cells[' '.join(td[0].text().split())] = td[1]
    title_cell = cells.get('标题')
    title = one(a for a in title_cell.all('a') if a.attrs.get('title') == '复制标题').attrs['data-clipboard-text'] if title_cell else ''
    doi_cell = cells.get('DOI')
    dois = [n.attrs['data-clipboard-text'] for n in doi_cell.all() if n.attrs.get('data-clipboard-text')] if doi_cell else []
    try:
        doi = doi_normalize(dois[0]) if len(dois) == 1 else None
    except ValueError:
        doi = None
    owner_cell = cells.get('求助人')
    owners = [a.attrs.get('data-id') for a in owner_cell.all('a', 'show-user-tips')] if owner_cell else []
    owner = owners[0] if len(owners) == 1 else None
    note = ' '.join(cells['备注'].text().split()) if '备注' in cells else ''
    # Do not interpret arbitrary user notes as executable instructions or assume
    # they request the ordinary article. Supplements and notices are not articles.
    special = bool(note) or any(w in title_cell.text().lower() for w in
                              ('补充材料', 'supplement', '应助此贴', '禁止应助')) if title_cell else True
    token = next((n.attrs.get('content') for n in root.all('meta') if n.attrs.get('name') == 'csrf-token'), None)
    return dict(id=expected_id, title=title, doi=doi, owner=owner, state=state,
                special=special, csrf=token,
                upload_form='new ss.SimpleUpload' in html and '/assist/upload-request' in html,
                login_required='need-login-tips' in html)


def eligible(item, own_id):
    return (item['state'] == 'waiting' and item['doi'] and item['title'] and
            item['owner'] and item['owner'] != own_id and not item['special'])


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Halt('unexpected_redirect')


def safe_host(url, allowed):
    p = urlparse.urlsplit(url)
    if (p.scheme != 'https' or not p.hostname or (allowed and p.hostname not in allowed) or p.port not in (None, 443)
            or p.username or p.password or p.query or p.fragment):
        raise Halt('upload_host_not_approved')
    # Defence in depth; deploy with network egress rules too (DNS can change).
    addresses = socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise Halt('nonpublic_upload_address')
    return url


class Site:
    def __init__(self, interval=3):
        self.jar = http.cookiejar.CookieJar()  # memory only; never browser cookie export
        self.opener = request.build_opener(NoRedirect(), request.HTTPCookieProcessor(self.jar))
        self.interval, self.last = interval, 0.0

    def call(self, path, fields=None):
        if not path.startswith('/') or path.startswith('//'):
            raise Halt('unexpected_site_path')
        if STOP.wait(max(0, self.interval - (time.monotonic() - self.last))):
            raise Halt('stopped')
        self.last = time.monotonic()
        data = urlparse.urlencode(fields).encode() if fields is not None else None
        headers = {'User-Agent': 'ScansciAssist/0.1 (research helper)', 'Referer': BASE + '/'}
        if fields is not None:
            headers.update({'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'XMLHttpRequest'})
        try:
            with self.opener.open(request.Request(BASE + path, data=data, headers=headers), timeout=25) as r:
                raw = r.read(4 * 1024 * 1024 + 1)
                if r.headers.get('cf-mitigated') == 'challenge' or len(raw) > 4 * 1024 * 1024:
                    raise Halt('challenge_or_oversize_response')
        except urllib.error.HTTPError as e:
            raise Halt('site_http_' + str(e.code)) from None
        text = raw.decode('utf-8')
        if '<title>Just a moment' in text or 'cf-chl-' in text:
            raise Halt('browser_verification_required')
        if fields is None:
            return text
        try:
            result = json.loads(text)
            if not isinstance(result, dict) or type(result.get('code')) is not int:
                raise ValueError()
            return result
        except (ValueError, TypeError):
            raise Halt('unexpected_json_response') from None

    def login(self, username, password):
        root = Page(self.call('/site/login')).root
        token = one(n for n in root.all('meta') if n.attrs.get('name') == 'csrf-token').attrs['content']
        result = self.call('/site/login', {'_csrf': token, 'email': username, 'password': password, 'remember': ''})
        if result['code'] != 0:
            raise Halt('login_or_verification_required')
        html = self.call('/my/assist-give')
        if 'able-head-user-guest' in html or 'id="login-form"' in html:
            raise Halt('login_not_confirmed')

    def detail(self, ident):
        return detail_page(self.call('/assist/detail?id=' + ident), ident)


class Ledger:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.root / 'assist.sqlite', timeout=10)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, state TEXT, updated REAL, data TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS events (at REAL, id TEXT, state TEXT)')
        self.db.commit()

    def save(self, ident, state, data):
        # Tokens, cookies, signed upload policies and passwords never enter ledger.
        def sanitize(value):
            if isinstance(value, dict):
                return {k: sanitize(v) for k, v in value.items() if k not in
                        ('csrf', 'password', 'policy', 'cookie', 'signature', 'callback', 'accessid')}
            if isinstance(value, list):
                return [sanitize(v) for v in value]
            return value
        clean = sanitize(data)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?)', (ident, state, time.time(), json.dumps(clean)))
            self.db.execute('INSERT INTO events VALUES (?,?,?)', (time.time(), ident, state))

    def state(self, ident):
        return self.db.execute('SELECT state,updated FROM jobs WHERE id=?', (ident,)).fetchone()

    def attempted_today(self):
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return self.db.execute("SELECT COUNT(DISTINCT id) FROM events WHERE state='posting_uncertain' AND at>=?", (today,)).fetchone()[0]

    def summary(self):
        return dict(self.db.execute('SELECT state,COUNT(*) FROM jobs GROUP BY state'))


def resolve(root, doi):
    """Reuse existing free lookup, but require additional redistribution evidence."""
    from free_sources import FreeSources
    from pipeline import crossref, validate_pdf
    from scihub_oa import license_evidence
    from pypdf import PdfReader
    meta = crossref(doi)
    proof = license_evidence(meta)
    if not proof:
        return dict(status='rights_not_verified', doi=doi)
    # Original unchanged BY-SA copies may be shared under the same license.
    engine = FreeSources(root, seconds=65, scihub_oa=False)
    found = engine.acquire(meta)
    if found.get('status') != 'verified':
        return dict(status='no_verified_pdf', doi=doi, events=found.get('events', []))
    path = Path(found['pdf'])
    check = validate_pdf(path, meta)
    if check['status'] != 'verified' or path.stat().st_size > 50 * 1024 * 1024:
        return dict(status='identity_or_size_needs_review', doi=doi)
    # Match the license in the actual file too. A bare OA flag, personal subscription
    # or a license for a different version is not permission to redistribute this PDF.
    texts = '\n'.join(p.extract_text() or '' for p in PdfReader(path).pages)
    compact = re.sub(r'\s+', '', texts).lower()
    license_path = urlparse.urlsplit(proof['license_url']).path.rstrip('/')
    if 'creativecommons.org' + license_path not in compact:
        return dict(status='file_license_not_confirmed', doi=doi)
    if any(s in compact for s in ('acceptedmanuscript', 'authoracceptedmanuscript', 'uncorrectedproof')):
        return dict(status='version_needs_review', doi=doi)
    return dict(status='ready', doi=doi, metadata=meta, pdf=str(path), sha256=check['sha256'],
                validation=check, license=proof, source=found.get('source'), source_url=found.get('source_url'),
                attribution='Unmodified PDF; original authors, copyright and license notices retained.')


def multipart(fields, filename, data):
    boundary = 'scansci-' + uuid.uuid4().hex
    chunks = []
    for k, v in fields.items():
        if not re.fullmatch(r'[A-Za-z0-9_:]+', k):
            raise Halt('invalid_form_field')
        chunks.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode(), str(v).encode(), b'\r\n'])
    chunks.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/pdf\r\n\r\n'.encode(), data, f'\r\n--{boundary}--\r\n'.encode()])
    return b''.join(chunks), 'multipart/form-data; boundary=' + boundary


def upload(site, ledger, item, artifact, own_id, allowed_hosts):
    from pipeline import norm, validate_pdf
    ident = item['id']
    previous = ledger.state(ident)
    if previous and previous[0] in ('posting_uncertain', 'uploaded', 'submitted_unconfirmed'):
        raise Halt('duplicate_or_uncertain_write')
    current = site.detail(ident)
    if not eligible(current, own_id) or current['doi'] != artifact['doi']:
        ledger.save(ident, 'request_no_longer_eligible', item)
        return
    if norm(current['title']) != norm(artifact['metadata']['title']):
        ledger.save(ident, 'title_mismatch', item)
        return
    if not current['upload_form'] or current['login_required'] or not current['csrf']:
        raise Halt('upload_form_or_login_required')
    path = Path(artifact['pdf'])
    checked = validate_pdf(path, artifact['metadata'])
    raw = path.read_bytes()
    if checked['status'] != 'verified' or hashlib.sha256(raw).hexdigest() != artifact['sha256']:
        raise Halt('pdf_changed_since_verification')
    if not artifact.get('license') or len(raw) > 50 * 1024 * 1024:
        raise Halt('rights_or_size_check_failed')
    filename = 'article-' + artifact['sha256'][:20] + '.pdf'
    record = dict(item=item, artifact=artifact, filename=filename)
    # /upload-request itself can complete a deduplicated upload (code 10).
    # Record uncertainty BEFORE any potentially committing request.
    ledger.save(ident, 'posting_uncertain', record)
    res = site.call('/assist/upload-request?t=' + str(int(time.time() * 1000)),
                    {'_csrf': current['csrf'], 'assist_id': ident, 'filename': filename,
                     'file_md5': hashlib.md5(raw).hexdigest(), 'filesize': len(raw)})
    if res['code'] == 2:
        raise Halt('upload_verification_required')
    if res['code'] == 0:
        d = res.get('data')
        required = ('host', 'dir', 'randFilename', 'policy', 'accessid', 'callback', 'signature', 'filename', 'assist_id', 'user_id')
        if not isinstance(d, dict) or any(not isinstance(d.get(k), (str, int)) for k in required):
            raise Halt('upload_ticket_schema_changed')
        host = safe_host(d['host'], allowed_hosts)
        fields = {'key': d['dir'] + d['randFilename'], 'policy': d['policy'], 'OSSAccessKeyId': d['accessid'],
                  'success_action_status': '200', 'callback': d['callback'], 'signature': d['signature'],
                  'x:filename': d['filename'], 'x:assist_id': d['assist_id'], 'x:user_id': d['user_id']}
        body, content_type = multipart(fields, filename, raw)
        # Separate opener: no website cookies/password/CSRF sent to storage host.
        with request.build_opener(NoRedirect()).open(request.Request(host, data=body,
                headers={'Content-Type': content_type, 'User-Agent': 'ScansciAssist/0.1'}), timeout=60) as response:
            uploaded = json.loads(response.read(1024 * 1024))
        if not isinstance(uploaded, dict) or uploaded.get('code') != 0:
            raise Halt('upload_result_uncertain')
    elif res['code'] != 10:
        raise Halt('upload_rejected_needs_review')
    # The server acknowledged the upload. Do not claim acceptance or points yet.
    ledger.save(ident, 'uploaded', record)


def finite_positive(value):
    n = float(value)
    if not math.isfinite(n) or n <= 0:
        raise argparse.ArgumentTypeError('must be positive and finite')
    return n


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['scan', 'run', 'status', 'resolve'])
    parser.add_argument('--work-dir', required=True, type=Path)
    parser.add_argument('--doi')
    parser.add_argument('--own-user-id')
    parser.add_argument('--hours', type=finite_positive, default=5)
    parser.add_argument('--poll-seconds', type=finite_positive, default=120)
    parser.add_argument('--max-daily-uploads', type=int, default=10)
    parser.add_argument('--max-items-per-cycle', type=int, default=10)
    parser.add_argument('--allow-upload', action='store_true')
    parser.add_argument('--upload-host', action='append', default=[])
    parser.add_argument('--prompt-login', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: STOP.set())
    if args.max_daily_uploads < 1 or args.max_items_per_cycle < 1 or args.poll_seconds < 30:
        parser.error('positive limits and poll interval >=30 seconds required')
    if args.mode == 'resolve':
        from pipeline import doi_normalize
        print(json.dumps(resolve(args.work_dir, doi_normalize(args.doi or ''))))
        return
    ledger = Ledger(args.work_dir)
    if args.mode == 'status':
        print(json.dumps(ledger.summary())); return
    site = Site()
    if args.mode == 'scan':
        print(json.dumps(scan_page(site.call('/assist/index?status=waiting')), ensure_ascii=False)); return
    if not args.own_user_id or not re.fullmatch(r'[A-Za-z0-9]+', args.own_user_id):
        parser.error('--own-user-id is required to exclude your own requests')
    # One daemon owns this ledger; kernel releases lock after a crash. No stale lock deletion.
    import fcntl
    lock = (ledger.root / 'run.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise Halt('another_runner_owns_this_work_directory')
    if args.allow_upload:
        username = input('AbleSci email/username: ') if args.prompt_login else os.environ.get('ABLESCI_USERNAME', '')
        password = getpass.getpass('AbleSci password (not saved): ') if args.prompt_login else os.environ.pop('ABLESCI_PASSWORD', '')
        if not username or not password:
            raise Halt('credentials_required_use_local_prompt_or_secret_injection')
        site.login(username, password)
        password = None
    end = time.monotonic() + args.hours * 3600
    while time.monotonic() < end and not STOP.is_set():
        if args.allow_upload and ledger.attempted_today() >= args.max_daily_uploads:
            break
        started = time.monotonic()
        candidates = scan_page(site.call('/assist/index?status=waiting'))[:args.max_items_per_cycle]
        for entry in candidates:
            if STOP.is_set() or time.monotonic() >= end:
                break
            prior = ledger.state(entry['id'])
            if prior and not (args.allow_upload and prior[0] == 'ready') and (prior[0] not in ('no_verified_pdf', 'lookup_timeout') or time.time() - prior[1] < 21600):
                continue
            item = site.detail(entry['id'])
            if not eligible(item, args.own_user_id):
                ledger.save(entry['id'], 'skipped', item); continue
            try:
                result = subprocess.run([sys.executable, str(Path(__file__).resolve()), 'resolve',
                    '--work-dir', str(ledger.root / 'papers'), '--doi', item['doi']],
                    capture_output=True, text=True, timeout=min(120, max(1, end - time.monotonic())))
                artifact = json.loads(result.stdout) if result.returncode == 0 else {'status': 'lookup_failed'}
            except subprocess.TimeoutExpired:
                artifact = {'status': 'lookup_timeout'}
            ledger.save(entry['id'], artifact['status'], dict(item=item, artifact=artifact))
            if artifact['status'] == 'ready' and args.allow_upload:
                if ledger.attempted_today() >= args.max_daily_uploads:
                    break
                upload(site, ledger, item, artifact, args.own_user_id, set(args.upload_host))
        print(json.dumps({'elapsed_seconds': round(time.monotonic() - started, 2), 'counts': ledger.summary()}), flush=True)
        STOP.wait(min(args.poll_seconds, max(0, end - time.monotonic())))
    print(json.dumps({'stopped': True, 'counts': ledger.summary()}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Do not print arbitrary response bodies, signed tickets, passwords or URLs.
        print(json.dumps({'status': 'needs_review', 'reason': str(error) if isinstance(error, Halt) else type(error).__name__}), file=sys.stderr)
        sys.exit(2)
