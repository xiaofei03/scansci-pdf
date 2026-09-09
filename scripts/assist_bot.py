#!/usr/bin/env python3
"""AbleSci helping client. HTTP default; optional local Chrome session transport.

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
import importlib
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
TRACE = {}
TRACE_FILE = None
VOID = set('area base br col embed hr img input link meta param source track wbr'.split())


class Halt(RuntimeError):
    """An authentication, schema or uncertain-write condition requiring review."""


def progress(stage, **facts):
    TRACE.clear()
    TRACE.update(stage=stage, **facts)
    log_event(dict(at=time.time(), **TRACE))


def log_event(event):
    line = json.dumps(event)
    print(line, file=sys.stderr, flush=True)
    if TRACE_FILE is not None:
        try:
            with TRACE_FILE.open('a', encoding='utf-8') as stream:
                stream.write(line + '\n')
        except OSError:
            pass  # Never hide the real network failure behind a logging failure.


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
        route = urlparse.urlsplit(path).path
        method = 'GET' if fields is None else 'POST'
        for attempt in range(1, 3 if fields is None else 2):
            progress('site_request', method=method, route=route, attempt=attempt)
            started = time.monotonic()
            try:
                result = self._call_once(path, fields)
            except (TimeoutError, urllib.error.URLError) as error:
                timeout = isinstance(error, TimeoutError) or isinstance(getattr(error, 'reason', None), TimeoutError)
                if timeout and fields is None and attempt == 1:
                    progress('read_timeout_retry', method=method, route=route)
                    continue
                raise Halt('site_timeout' if timeout else 'site_transport_error') from None
            progress('site_response', method=method, route=route,
                     seconds=round(time.monotonic() - started, 2))
            return result

    def _call_once(self, path, fields=None):
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
        logout = [a for a in Page(html).root.all('a') if
                  urlparse.urljoin(BASE, a.attrs.get('href', '')) == BASE + '/site/logout']
        if 'able-head-user-guest' in html or 'id="login-form"' in html or not logout:
            raise Halt('login_not_confirmed')

    def detail(self, ident):
        return detail_page(self.call('/assist/detail?id=' + ident), ident)

    def confirm_account(self, expected):
        root = Page(self.call('/my/home')).root
        ids = set()
        for a in root.all('a'):
            p = urlparse.urlsplit(urlparse.urljoin(BASE, a.attrs.get('href', '')))
            if p.netloc == 'www.ablesci.com' and p.path == '/user/home':
                ids.update(urlparse.parse_qs(p.query).get('id', []))
        if ids != {expected}:
            raise Halt('logged_in_account_id_not_confirmed')
        progress('account_confirmed', own_user_id=expected)


class BrowserSite(Site):
    """Same-origin HTTP in an existing logged-in tab, no clicks/cookie extraction.

    Uses Apple Events only to execute bounded fetches. The browser keeps cookies.
    Does not open/navigate tabs, change browser preferences or retry POSTs.
    """
    def __init__(self, tab_url=None, interval=3):
        self.interval, self.last = interval, 0.0
        self.tab_url, self.target = tab_url, None
        p = urlparse.urlsplit(tab_url or BASE + '/')
        if p.scheme != 'https' or p.netloc != 'www.ablesci.com' or p.username or p.password:
            raise Halt('browser_tab_must_be_ablesci_https')
        if sys.platform != 'darwin':
            raise Halt('browser_session_requires_macos')

    def chrome(self, javascript):
        script = '''on run argv
tell application "Google Chrome"
set matches to {}
repeat with w in windows
repeat with t in tabs of w
if URL of t starts with "https://www.ablesci.com/" then
if (item 2 of argv) is not "" then
if (id of w as text) is (item 2 of argv) and (id of t as text) is (item 3 of argv) then set end of matches to {id of w, id of t}
else if (item 1 of argv) is "" or URL of t is (item 1 of argv) then
set end of matches to {id of w, id of t}
end if
end if
end repeat
end repeat
if (count of matches) is 0 then return "__TAB_NOT_UNIQUE__"
if (item 1 of argv) is not "" and (item 2 of argv) is "" and (count of matches) is not 1 then return "__TAB_NOT_UNIQUE__"
set targetIds to item 1 of matches
repeat with w in windows
if (id of w as text) is (item 1 of targetIds as text) then
repeat with t in tabs of w
if (id of t as text) is (item 2 of targetIds as text) then
set resultText to execute t javascript (item 4 of argv)
return (id of w as text) & linefeed & (id of t as text) & linefeed & resultText
end if
end repeat
end if
end repeat
return "__TAB_NOT_UNIQUE__"
end tell
end run'''
        try:
            output = subprocess.run(['osascript', '-e', script, self.tab_url or '',
                                     *(self.target or ('', '')), javascript],
                                    capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            raise Halt('browser_command_timeout') from None
        if output.returncode:
            # AppleScript errors can echo JS containing CSRF: never expose stderr.
            raise Halt('browser_automation_unavailable')
        if output.stdout.strip() == '__TAB_NOT_UNIQUE__':
            raise Halt('browser_tab_missing_or_ambiguous')
        parts = output.stdout.strip().split('\n', 2)
        if len(parts) != 3 or not all(re.fullmatch(r'\d+', n) for n in parts[:2]):
            raise Halt('browser_command_schema_changed')
        self.target = tuple(parts[:2])
        return parts[2]

    @staticmethod
    def check_path(path, fields):
        p = urlparse.urlsplit(path)
        if p.scheme or p.netloc or p.fragment or not path.startswith('/') or path.startswith('//'):
            raise Halt('unexpected_site_path')
        allowed = ('/', '/assist/index', '/assist/detail', '/my/assist-give', '/my/home')
        if (fields is None and p.path not in allowed) or (fields is not None and p.path != '/assist/upload-request'):
            raise Halt('browser_route_not_allowed')

    def _call_once(self, path, fields=None):
        self.check_path(path, fields)
        if STOP.wait(max(0, self.interval - (time.monotonic() - self.last))):
            raise Halt('stopped')
        self.last = time.monotonic()
        key = '__scansci_http_' + uuid.uuid4().hex
        config = json.dumps({'key': key, 'path': path, 'fields': fields})
        js = '''(() => {
const c = CONFIG;
if (location.origin !== "https://www.ablesci.com") return "wrong_origin";
const ac = new AbortController();
const slot = window[c.key] = {done:false, cancel:() => ac.abort()};
const timer = setTimeout(() => ac.abort(), 30000);
(async () => {
try {
const opts = {credentials:"same-origin", redirect:"error", signal:ac.signal};
if (c.fields !== null) Object.assign(opts,{method:"POST", body:new URLSearchParams(c.fields),
headers:{"Content-Type":"application/x-www-form-urlencoded", "X-Requested-With":"XMLHttpRequest"}});
const r = await fetch(c.path, opts);
if (!r.ok) {slot.error="site_http_"+r.status; return;}
if (r.headers.get("cf-mitigated") === "challenge") {slot.error="browser_verification_required"; return;}
const reader=r.body.getReader(), chunks=[]; let size=0;
while(true){const part=await reader.read();if(part.done)break;
size+=part.value.length;if(size>4194304){await reader.cancel();slot.error="response_too_large";return;}
chunks.push(part.value);}
const bytes=new Uint8Array(size);let offset=0;
for(const part of chunks){bytes.set(part,offset);offset+=part.length;}
slot.text=new TextDecoder().decode(bytes);
} catch(e) {slot.error=e.name === "AbortError" ? "timeout" : "browser_fetch_failed";}
finally {clearTimeout(timer); slot.done=true;}
})();return "started";
})()'''.replace('CONFIG', config)
        if self.chrome(js) != 'started':
            raise Halt('browser_origin_changed')
        end = time.monotonic() + 40
        try:
            while time.monotonic() < end:
                if STOP.wait(0.4):
                    raise Halt('stopped')
                raw = self.chrome('JSON.stringify((() => {const s=window[' + json.dumps(key) +
                                  '];return s ? {done:s.done,error:s.error,text:s.text} : {error:"browser_context_lost",done:true};})())')
                try:
                    result = json.loads(raw)
                except ValueError:
                    raise Halt('browser_context_lost') from None
                if not result.get('done'):
                    continue
                if result.get('error') == 'timeout':
                    raise TimeoutError()
                if result.get('error'):
                    raise Halt(result['error'])
                text = result.get('text', '')
                if '<title>Just a moment' in text or 'cf-chl-' in text:
                    raise Halt('browser_verification_required')
                if fields is None:
                    return text
                try:
                    data = json.loads(text)
                    if not isinstance(data, dict) or type(data.get('code')) is not int:
                        raise ValueError()
                    return data
                except ValueError:
                    raise Halt('unexpected_json_response') from None
            raise TimeoutError()
        finally:
            try:
                self.chrome('(() => {const k=' + json.dumps(key) +
                            ';window[k]?.cancel();delete window[k];return "cleared";})()')
            except Halt:
                pass  # Do not conceal original failure or retry an uncertain POST.

    def confirm_session(self):
        html = self.call('/my/assist-give')
        root = Page(html).root
        if not any(urlparse.urljoin(BASE, a.attrs.get('href', '')) == BASE + '/site/logout'
                   for a in root.all('a')) or 'able-head-user-guest' in html:
            raise Halt('browser_login_required')


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

    def candidates(self, fresh, live, limit, now=None):
        """Resume saved work even after the request disappears from the first page."""
        now = time.time() if now is None else now
        saved = self.db.execute('SELECT id,state,updated FROM jobs ORDER BY updated,id').fetchall()
        rows = {ident: (state, updated) for ident, state, updated in saved}

        def runnable(ident):
            previous = rows.get(ident)
            if previous is None:
                return True
            state, updated = previous
            return (state == 'run_deadline' or (live and state == 'ready') or
                    (state in ('no_verified_pdf', 'lookup_timeout', 'lookup_failed') and now - updated >= 21600))

        ordered = [{'id': ident} for ident, _, _ in saved if runnable(ident)] + fresh
        unique = {}
        for entry in ordered:
            if runnable(entry['id']):
                unique.setdefault(entry['id'], entry)
        return list(unique.values())[:limit]


def lookup_child(root, doi, deadline):
    """Stop a lookup on SIGTERM promptly; never orphan a long PDF parser process."""
    command = [sys.executable, str(Path(__file__).resolve()), 'resolve',
               '--work-dir', str(root), '--doi', doi]
    end = min(deadline, time.monotonic() + 120)
    if STOP.is_set():
        raise Halt('stopped')
    if time.monotonic() >= end:
        return {'status': 'run_deadline' if time.monotonic() >= deadline else 'lookup_timeout'}
    child_env = {k: v for k, v in os.environ.items() if k not in ('ABLESCI_PASSWORD', 'ABLESCI_USERNAME')}
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=child_env)
    try:
        while True:
            if STOP.is_set():
                raise Halt('stopped')
            remaining = end - time.monotonic()
            if remaining <= 0:
                return {'status': 'run_deadline' if time.monotonic() >= deadline else 'lookup_timeout'}
            try:
                stdout, stderr = child.communicate(timeout=min(0.5, remaining))
            except subprocess.TimeoutExpired:
                continue
            if child.returncode != 0:
                reason = 'child_process_failed'
                try:
                    candidate = json.loads(stderr.strip().splitlines()[-1]).get('reason', '')
                    if isinstance(candidate, str) and re.fullmatch(r'[A-Za-z0-9_]{1,100}', candidate):
                        reason = candidate
                except (ValueError, IndexError, AttributeError):
                    pass
                return {'status': 'lookup_failed', 'reason': reason}
            try:
                result = json.loads(stdout)
                if not isinstance(result, dict) or not isinstance(result.get('status'), str):
                    raise ValueError()
                return result
            except (ValueError, TypeError):
                return {'status': 'lookup_failed'}
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()


def reconcile(site, ledger, limit=10):
    """Observe existing writes only. A closed request does not prove our acceptance."""
    rows = ledger.db.execute("SELECT id,state,data FROM jobs WHERE state IN "
                             "('posting_uncertain','uploaded','submitted_unconfirmed') ORDER BY updated LIMIT ?",
                             (limit,)).fetchall()
    result = []
    for ident, state, payload in rows:
        if STOP.is_set():
            break
        current = site.detail(ident)
        record = json.loads(payload)
        expected = record.get('artifact', {}).get('doi')
        observation = {'checked_at': time.time(), 'request_state': current['state'],
                       'doi_matches': bool(expected and current['doi'] == expected),
                       'acceptance': 'not_verified', 'points_earned': None}
        record['remote_observation'] = observation
        ledger.save(ident, state, record)
        result.append({'id': ident, 'local_state': state, **observation})
    return result


def authenticate(site, prompt):
    # Pop even in prompt mode so an inherited secret cannot leak into child jobs.
    supplied = os.environ.pop('ABLESCI_PASSWORD', '')
    if isinstance(site, BrowserSite):
        site.confirm_session()
        progress('authenticated', transport='chrome_session', session_exported=False)
        return
    if prompt and not sys.stdin.isatty():
        raise Halt('login_prompt_requires_private_interactive_terminal')
    username = input('AbleSci email/username: ') if prompt else os.environ.get('ABLESCI_USERNAME', '')
    password = getpass.getpass('AbleSci password (not saved): ') if prompt else supplied
    if not username or not password:
        raise Halt('credentials_required_use_local_prompt_or_secret_injection')
    site.login(username, password)
    progress('authenticated', transport='http', session_persisted=False)


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
        progress('storage_upload', request_id=ident, host=urlparse.urlsplit(host).hostname)
        with request.build_opener(NoRedirect()).open(request.Request(host, data=body,
                headers={'Content-Type': content_type, 'User-Agent': 'ScansciAssist/0.1'}), timeout=60) as response:
            uploaded = json.loads(response.read(1024 * 1024))
        if not isinstance(uploaded, dict) or uploaded.get('code') != 0:
            raise Halt('upload_result_uncertain')
    elif res['code'] != 10:
        raise Halt('upload_rejected_needs_review')
    # The server acknowledged the upload. Do not claim acceptance or points yet.
    ledger.save(ident, 'uploaded', record)
    progress('upload_acknowledged', request_id=ident, acceptance='not_verified')


def finite_positive(value):
    n = float(value)
    if not math.isfinite(n) or n <= 0:
        raise argparse.ArgumentTypeError('must be positive and finite')
    return n


def require_pdf_runtime():
    """Fail before login rather than marking every lookup failed in a wrong Python."""
    try:
        pdf = importlib.import_module('pypdf')
    except ImportError:
        raise Halt('pdf_runtime_missing_use_python_with_skill_requirements') from None
    try:
        version = tuple(int(n) for n in pdf.__version__.split('.')[:2])
    except (AttributeError, ValueError):
        raise Halt('pdf_runtime_version_unrecognized') from None
    if not (version >= (6, 10) and version < (7, 0)):
        raise Halt('pdf_runtime_version_requires_pypdf_6_10_to_below_7')
    return {'pypdf_version': pdf.__version__}


def main():
    global TRACE_FILE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['scan', 'run', 'status', 'resolve', 'login-check', 'reconcile'])
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
    parser.add_argument('--browser-session', action='store_true', help='Use existing Chrome login on macOS, no clicks or cookie export')
    parser.add_argument('--browser-tab-url', help='Optional exact existing AbleSci tab URL; otherwise pin the first existing AbleSci tab')
    args = parser.parse_args()
    if args.browser_session and args.prompt_login:
        parser.error('--browser-session and --prompt-login are mutually exclusive')
    os.umask(0o077)
    if args.max_daily_uploads < 1 or args.max_items_per_cycle < 1 or args.poll_seconds < 30:
        parser.error('positive limits and poll interval >=30 seconds required')
    if args.mode in ('run', 'resolve', 'reconcile'):
        runtime = require_pdf_runtime()
        if args.mode == 'run':
            print(json.dumps({'stage': 'runtime_ready', **runtime}), flush=True)
    if args.mode == 'resolve':
        from pipeline import doi_normalize
        print(json.dumps(resolve(args.work_dir, doi_normalize(args.doi or ''))))
        return
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: STOP.set())
    ledger = Ledger(args.work_dir)
    if args.mode in ('run', 'login-check', 'reconcile'):
        TRACE_FILE = ledger.root / 'runtime.jsonl'
    if args.mode == 'status':
        print(json.dumps(ledger.summary())); return
    site = BrowserSite(args.browser_tab_url) if args.browser_session else Site()
    if args.mode == 'login-check':
        authenticate(site, args.prompt_login)
        print(json.dumps({'logged_in': True, 'session_persisted': False, 'uploaded': 0})); return
    if args.mode == 'scan':
        print(json.dumps(scan_page(site.call('/assist/index?status=waiting')), ensure_ascii=False)); return
    if args.mode == 'run' and (not args.own_user_id or not re.fullmatch(r'[A-Za-z0-9]+', args.own_user_id)):
        parser.error('--own-user-id is required to exclude your own requests')
    # One daemon owns this ledger; kernel releases lock after a crash. No stale lock deletion.
    import fcntl
    lock = (ledger.root / 'run.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise Halt('another_runner_owns_this_work_directory')
    if args.mode == 'reconcile':
        print(json.dumps(reconcile(site, ledger, args.max_items_per_cycle))); return
    if args.allow_upload:
        authenticate(site, args.prompt_login)
        site.confirm_account(args.own_user_id)
    end = time.monotonic() + args.hours * 3600
    while time.monotonic() < end and not STOP.is_set():
        if args.allow_upload:
            reconcile(site, ledger, args.max_items_per_cycle)
        if args.allow_upload and ledger.attempted_today() >= args.max_daily_uploads:
            break
        started = time.monotonic()
        fresh = scan_page(site.call('/assist/index?status=waiting'))
        candidates = ledger.candidates(fresh, args.allow_upload, args.max_items_per_cycle)
        for entry in candidates:
            if STOP.is_set() or time.monotonic() >= end:
                break
            item = site.detail(entry['id'])
            if not eligible(item, args.own_user_id):
                ledger.save(entry['id'], 'skipped', item); continue
            if end - time.monotonic() < 5:
                ledger.save(entry['id'], 'run_deadline', dict(item=item))
                break
            progress('pdf_lookup', request_id=entry['id'], doi=item['doi'])
            artifact = lookup_child(ledger.root / 'papers', item['doi'], end)
            ledger.save(entry['id'], artifact['status'], dict(item=item, artifact=artifact))
            progress('pdf_result', request_id=entry['id'], result=artifact['status'])
            if artifact['status'] == 'run_deadline':
                break
            if artifact['status'] == 'ready' and args.allow_upload:
                if ledger.attempted_today() >= args.max_daily_uploads:
                    break
                upload(site, ledger, item, artifact, args.own_user_id, set(args.upload_host))
        print(json.dumps({'elapsed_seconds': round(time.monotonic() - started, 2), 'counts': ledger.summary()}), flush=True)
        if args.allow_upload and ledger.attempted_today() >= args.max_daily_uploads:
            break
        STOP.wait(min(args.poll_seconds, max(0, end - time.monotonic())))
    print(json.dumps({'stopped': True, 'counts': ledger.summary()}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Do not print arbitrary response bodies, signed tickets, passwords or URLs.
        log_event({'at': time.time(), 'status': 'needs_review',
                   'reason': str(error) if isinstance(error, Halt) else type(error).__name__, 'context': dict(TRACE)})
        sys.exit(2)
