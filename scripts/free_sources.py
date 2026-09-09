"""Bounded lawful PDF lookup. No browser, Zotero writes, cookies or paid requests.

Credentials come from the environment or a private user config, never job records.
Network deadlines are cooperative: DNS/one blocking socket read may overrun them.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import getpass
import hashlib
from html.parser import HTMLParser
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse as urlparse
import urllib.request as request

CONFIG = Path.home() / '.config/scansci-pdf/config.json'
ENV = {'email': 'SCANSCI_EMAIL', 'openalex_api_key': 'OPENALEX_API_KEY',
       'elsevier_api_key': 'ELSEVIER_API_KEY', 'elsevier_insttoken': 'ELSEVIER_INSTTOKEN'}
SECRET_HEADERS = ('X-els-apikey', 'X-els-insttoken', 'Authorization')


def settings(path=None):
    path = Path(path or os.environ.get('SCANSCI_CONFIG', CONFIG)).expanduser()
    data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    out = {key: os.environ.get(env, data.get(key, '')).strip() for key, env in ENV.items()}
    if out['email'] and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', out['email']):
        raise ValueError('Invalid contact email in private scansci config')
    return out


def configure():
    path = Path(os.environ.get('SCANSCI_CONFIG', CONFIG)).expanduser()
    data = settings(path)
    print('Private local configuration. Enter keeps existing values; - clears a value.')
    for key in ENV:
        prompt = f'{key} ({"configured" if data[key] else "not configured"}): '
        value = input(prompt) if key == 'email' else getpass.getpass(prompt)
        if value.strip(): data[key] = '' if value.strip() == '-' else value.strip()
    if data['email'] and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', data['email']):
        raise ValueError('Invalid email; configuration not changed')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink(): raise ValueError('Refusing symlink configuration target')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    print(f'Saved private config: {path}. Do not upload this file or its values.')


def public_url(url):
    p = urlparse.urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('unsupported_url')
    host = p.hostname.lower()
    if host == 'localhost' or host.endswith(('.localhost', '.local')):
        raise ValueError('nonpublic_url')
    try:
        if not ipaddress.ip_address(host).is_global: raise ValueError('nonpublic_url')
    except ValueError as e:
        if str(e) == 'nonpublic_url': raise
    return urlparse.urlunsplit(p._replace(fragment=''))


class SafeRedirect(request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts=None):
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        if self.allowed_hosts is not None: source_url(newurl, self.allowed_hosts)
        result = super().redirect_request(req, fp, code, msg, headers, newurl)
        old, new = urlparse.urlsplit(req.full_url), urlparse.urlsplit(newurl)
        if old.netloc != new.netloc or old.scheme != new.scheme:
            for h in SECRET_HEADERS: result.remove_header(h)
            # Never follow a provider key copied into a cross-origin redirect.
            if any(k.lower() in ('api_key', 'apikey', 'insttoken', 'access_token')
                   for k, _ in urlparse.parse_qsl(new.query)):
                raise ValueError('credential_redirect_blocked')
        return result


def source_url(url, allowed_hosts):
    p = urlparse.urlsplit(public_url(url))
    if p.scheme != 'https' or p.hostname not in allowed_hosts or p.port not in (None,443):
        raise ValueError('source_url_not_allowed')


def _download_bytes(url, headers, deadline, limit, timeout, context=None, allowed_hosts=None):
    public_url(url)
    if allowed_hosts is not None: source_url(url, allowed_hosts)
    remaining = deadline - time.monotonic()
    if remaining <= 0: raise TimeoutError('lookup_deadline')
    req = request.Request(url, headers={'User-Agent': 'ScansciPDF/0.2 (personal research)', **headers})
    handlers = [SafeRedirect(allowed_hosts)]
    if context is not None: handlers.append(request.HTTPSHandler(context=context))
    with request.build_opener(*handlers).open(req, timeout=min(timeout, remaining)) as r:
        parts, count = [], 0
        while True:
            if time.monotonic() >= deadline: raise TimeoutError('lookup_deadline')
            chunk = r.read1(min(65536, limit + 1 - count))
            if not chunk: break
            count += len(chunk)
            if count > limit: raise ValueError('response_too_large')
            parts.append(chunk)
        return b''.join(parts), r.geturl()


def tls_record_failure(error):
    """Only the observed record-protocol failure; never a certificate/identity error."""
    cause = getattr(error, 'reason', None)
    cause = cause if isinstance(cause, BaseException) else error
    return (isinstance(cause, ssl.SSLError) and
            not isinstance(cause, ssl.SSLCertVerificationError) and
            'record layer failure' in str(cause).lower())


def download_bytes(url, headers, deadline, limit, timeout=12, transport_events=None, allowed_hosts=None):
    try:
        return _download_bytes(url, headers, deadline, limit, timeout, allowed_hosts=allowed_hosts)
    except (ssl.SSLError, urllib.error.URLError) as error:
        if not tls_record_failure(error) or time.monotonic() >= deadline: raise
        # Narrow per-request compatibility retry, no global TLS/proxy/CA change.
        # Keep system trust roots, certificate verification and hostname checking.
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        if transport_events is not None:
            transport_events.append(dict(status='tls12_retry', reason='tls_record_error'))
        # Discard any partial bytes. Same deadline/limits and safe redirect handler.
        return _download_bytes(url, headers, deadline, limit, timeout, context=context, allowed_hosts=allowed_hosts)


def identity(value):
    return re.sub(r'^https?://(?:dx\.)?doi\.org/', '', value or '', flags=re.I).lower()


class MetaTags(HTMLParser):
    def __init__(self):
        super().__init__(); self.values = {}
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'meta': self.values[a.get('name', '').lower()] = a.get('content', '')


class FreeSources:
    def __init__(self, root, config=None, seconds=75, scihub_oa=None):
        self.root = Path(root) / 'free_sources'
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = settings() if config is None else {k: config.get(k, '') for k in ENV}
        self.seconds = seconds
        self.scihub_oa = (os.environ.get('SCANSCI_SCIHUB_OA','1').lower() not in ('0','false','off')
                         if scihub_oa is None else bool(scihub_oa))
        self.lock = threading.Lock()
        self.host_locks, self.cooldowns = {}, {}
        self.challenge_hosts = set()

    def fetch(self, source, url, headers, deadline, events, pdf=False):
        host = urlparse.urlsplit(url).hostname or ''
        with self.lock:
            semaphore = self.host_locks.setdefault(host, threading.BoundedSemaphore(2))
            cooldown = self.cooldowns.get(host, 0)
            challenged = host in self.challenge_hosts
        if challenged:
            events.append(dict(source=source, status='challenge_host_skipped', host=host)); return None
        if cooldown > time.monotonic():
            events.append(dict(source=source, status='host_cooldown', host=host)); return None
        start = time.monotonic()
        if not semaphore.acquire(timeout=max(0, deadline-start)):
            events.append(dict(source=source, status='deadline')); return None
        transport_events = []
        try:
            restricted = None
            if source == 'scihub_oa':
                from scihub_oa import HOST
                restricted = {HOST}
                source_url(url, restricted)
                # No API keys, cookies or personal headers enter this adapter.
                headers = {'Accept': 'application/pdf' if pdf else 'text/html'}
            data, final = download_bytes(url, headers, deadline, 60*1024*1024 if pdf else 4*1024*1024,
                                         transport_events=transport_events, allowed_hosts=restricted)
            events.append(dict(source=source, status='received', host=host, bytes=len(data),
                               seconds=round(time.monotonic()-start, 3), phase='pdf' if pdf else 'discovery'))
            return data, final
        except Exception as e:
            code = getattr(e, 'code', None)
            reason = ({400:'bad_request', 401:'authentication_required', 403:'access_denied', 404:'not_found',
                       429:'rate_limited'}.get(code) or
                      ('server_error' if code and code >= 500 else
                       'timeout' if isinstance(e, (TimeoutError, socket.timeout)) or
                       isinstance(getattr(e, 'reason', None), (TimeoutError, socket.timeout)) else 'network_or_format_error'))
            cause = getattr(e, 'reason', None)
            cause = cause if isinstance(cause, BaseException) else e
            detail = {}
            if isinstance(cause, ssl.SSLCertVerificationError):
                reason = 'tls_certificate_error'
                detail['certificate_verify_code'] = cause.verify_code
            elif isinstance(cause, ssl.SSLError):
                reason = 'tls_record_error' if tls_record_failure(cause) else 'tls_handshake_error'
            if code == 403 and getattr(e, 'headers', None) and e.headers.get('cf-mitigated', '').lower() == 'challenge':
                reason = 'browser_verification_required'
                with self.lock:
                    self.challenge_hosts.add(host)
                    # On redirected requests, avoid mislabelling only the origin host.
                    final_host = urlparse.urlsplit(e.geturl()).hostname
                    if final_host: self.challenge_hosts.add(final_host)
                detail['action'] = 'use_other_source_or_user_verification'
            if code in (401, 429, 503):
                raw = e.headers.get('Retry-After', '') if getattr(e, 'headers', None) else ''
                delay = min(300, max(15, int(raw))) if raw.isdigit() else 60
                with self.lock: self.cooldowns[host] = time.monotonic()+delay
            if source == 'elsevier_api' and isinstance(e, urllib.error.HTTPError):
                try:
                    raw = e.read(8192)
                    try:
                        body = json.loads(raw)
                        error = body.get('error-response', body.get('service-error', {}).get('status', {}))
                    except json.JSONDecodeError:
                        import xml.etree.ElementTree as ET
                        error = {x.tag.rsplit('}',1)[-1]:x.text for x in ET.fromstring(raw).iter()}
                    message = str(error.get('error-message', error.get('statusText', '')))
                    for secret in self.config.values():
                        if secret: message = message.replace(secret, '[redacted]')
                    detail['provider_message'] = re.sub(r'https?://\S+', '[url]', message)[:240]
                    error_code = str(error.get('error-code', error.get('statusCode', '')))
                    if re.fullmatch(r'[A-Z_0-9-]{1,60}', error_code): detail['provider_error_code'] = error_code
                except Exception: pass
            events.append(dict(source=source, status=reason, http_status=code, host=host, **detail,
                               error_type=type(e).__name__, reason_type=type(getattr(e, 'reason', None)).__name__,
                               seconds=round(time.monotonic()-start, 3)))
            return None
        finally:
            events.extend(dict(source=source, host=host, **event) for event in transport_events)
            semaphore.release()

    def provider(self, source, meta, deadline):
        events, candidates = [], []
        doi = meta['doi']; cfg = self.config
        quoted = urlparse.quote(doi, safe='')
        if source == 'unpaywall' and not cfg['email']:
            return [], [dict(source=source, status='not_configured')]
        headers = {}
        if source == 'openalex':
            url = 'https://api.openalex.org/works/https://doi.org/' + quoted
            if cfg['openalex_api_key']: headers['Authorization'] = 'Bearer '+cfg['openalex_api_key']
        elif source == 'unpaywall':
            url = 'https://api.unpaywall.org/v2/' + quoted + '?' + urlparse.urlencode({'email':cfg['email']})
        else: url = meta['url']
        result = self.fetch(source, url, headers, deadline, events)
        if not result: return candidates, events
        try:
            data, final = result
            if source == 'publisher_metadata':
                from pipeline import norm
                parser = MetaTags(); parser.feed(data.decode('utf-8', errors='replace'))
                v = parser.values
                if identity(v.get('citation_doi')) != doi or norm(v.get('citation_title', '')) != norm(meta['title']):
                    raise ValueError('identity_mismatch')
                if v.get('citation_pdf_url'):
                    candidates.append(dict(source=source, url=urlparse.urljoin(final, v['citation_pdf_url']), version='unknown'))
            else:
                record = json.loads(data)
                if identity(record.get('doi')) != doi: raise ValueError('identity_mismatch')
                locations = (record.get('locations', []) if source == 'openalex' else
                             [record.get('best_oa_location')] + record.get('oa_locations', []))
                for loc in locations:
                    if not loc or (source == 'openalex' and not loc.get('is_oa')): continue
                    u = loc.get('pdf_url') if source == 'openalex' else loc.get('url_for_pdf')
                    if u: candidates.append(dict(source=source, url=u, version=loc.get('version', 'unknown')))
            events.append(dict(source=source, status='candidates', count=len(candidates)))
        except Exception:
            events.append(dict(source=source, status='invalid_metadata_or_identity'))
        return candidates, events

    def acquire(self, meta):
        from pipeline import validate_pdf
        start = time.monotonic(); deadline = start + self.seconds
        events, candidates = [], []
        # Cached bytes still pass the same identity/version checks on every reuse.
        doi_dir = self.root / hashlib.sha256(meta['doi'].encode()).hexdigest()[:20]
        doi_dir.mkdir(exist_ok=True)
        for path in sorted(doi_dir.glob('*.pdf')):
            info = path.with_suffix('.json')
            if not info.exists(): continue
            try:
                cached = json.loads(info.read_text(encoding='utf-8'))
                check = validate_pdf(path, meta)
                if check['status'] in ('verified', 'probably_correct'):
                    return dict(status='verified', pdf=str(path), validation=check, source=cached['source'],
                                source_url=cached['source_url'], source_version=cached.get('source_version','unknown'),
                                events=[dict(source='cache',status='verified')], seconds=round(time.monotonic()-start,3))
            except (ValueError, KeyError): pass
        discovery_deadline = min(deadline, start+18)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.provider, s, meta, discovery_deadline)
                       for s in ('unpaywall', 'openalex')]
            for f in futures:
                found, records = f.result(); candidates.extend(found); events.extend(records)
        candidates += [dict(source='publisher', url=x['URL'], version='unknown')
                       for x in meta.get('links', []) if x.get('content-type') == 'application/pdf']
        if self.config['elsevier_api_key'] and ('elsevier' in meta.get('publisher','').lower() or
                urlparse.urlsplit(meta['url']).hostname in ('www.sciencedirect.com','sciencedirect.com','linkinghub.elsevier.com')):
            candidates.append(dict(source='elsevier_api', url='https://api.elsevier.com/content/article/doi/'+
                                   urlparse.quote(meta['doi'],safe='/'), version='publishedVersion'))
        # Prefer reported publisher versions; never change PDF identity gates for a repository copy.
        candidates.sort(key=lambda c: c['version'] != 'publishedVersion')
        def ordered_candidates():
            yield from candidates
            # A slow publisher HTML page must not delay an already known PDF.
            if time.monotonic() < deadline:
                found, records = self.provider('publisher_metadata', meta, min(deadline,time.monotonic()+12))
                events.extend(records)
                yield from found
        seen = set()
        for c in ordered_candidates():
            if time.monotonic() >= deadline or len(seen) >= 6: break
            try: url = public_url(c['url'])
            except ValueError: continue
            if url in seen: continue
            seen.add(url)
            headers = {'Accept':'application/pdf'}
            if c['source'] == 'elsevier_api':
                headers['X-ELS-APIKey'] = self.config['elsevier_api_key']
                if self.config['elsevier_insttoken']: headers['X-ELS-Insttoken'] = self.config['elsevier_insttoken']
            result = self.fetch(c['source'], url, headers, deadline, events, pdf=True)
            if not result: continue
            data, final = result
            if not data.startswith(b'%PDF-'):
                events.append(dict(source=c['source'], status='not_pdf')); continue
            path = doi_dir / (hashlib.sha256(data).hexdigest()[:20] + '.pdf')
            path.write_bytes(data)
            check = validate_pdf(path, meta)
            events.append(dict(source=c['source'], status='validation', result=check['status'], reason=check.get('reason')))
            if check['status'] not in ('verified', 'probably_correct'): continue
            # Do not persist keys, email, signed URLs or arbitrary provider query values.
            p = urlparse.urlsplit(final)
            safe_url = urlparse.urlunsplit(p._replace(query='', fragment=''))
            out = dict(status='verified', pdf=str(path), validation=check, source=c['source'],
                       source_url=safe_url, source_version=c['version'])
            path.with_suffix('.json').write_text(json.dumps({k:out[k] for k in ('source','source_url','source_version')}), encoding='utf-8')
            return {**out, 'events':events, 'seconds':round(time.monotonic()-start,3)}
        result = self.acquire_scihub_oa(meta, deadline)
        result['events'] = events + result['events']
        result['seconds'] = round(time.monotonic()-start,3)
        result['deadline_reached'] = time.monotonic() >= deadline
        return result

    def acquire_scihub_oa(self, meta, deadline=None):
        from scihub_oa import acquire
        if not self.scihub_oa:
            return dict(status='unresolved',events=[dict(source='scihub_oa',status='disabled')],seconds=0)
        return acquire(self, meta, deadline if deadline is not None else time.monotonic()+self.seconds)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--configure', action='store_true')
    p.add_argument('--status', action='store_true')
    p.add_argument('--doi', action='append', default=[])
    p.add_argument('--work-dir')
    p.add_argument('--scihub-oa-only', action='store_true', help='Test only the license-gated optional source')
    p.add_argument('--no-scihub-oa', action='store_true', help='Disable the license-gated source')
    args = p.parse_args()
    if args.configure: configure(); return
    cfg = settings()
    if args.status:
        print(json.dumps({k:bool(v) for k,v in cfg.items()})); return
    if not args.doi or not args.work_dir: p.error('provide --doi and --work-dir, or --configure/--status')
    from pipeline import crossref, doi_normalize
    engine = FreeSources(args.work_dir, cfg, scihub_oa=False if args.no_scihub_oa else None)
    # Read-only metadata/PDF test: never modifies Zotero or creates AbleSci requests.
    metas = [crossref(doi_normalize(d)) for d in args.doi]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(engine.acquire_scihub_oa if args.scihub_oa_only else engine.acquire, metas))
    for meta, result in zip(metas, results):
        print(json.dumps({'doi':meta['doi'], **result}, ensure_ascii=False))


if __name__ == '__main__': main()
