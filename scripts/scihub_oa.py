"""License-gated supplementary copy retrieval; no official API or subscription access.

Only already-effective Crossref version-of-record CC BY / BY-SA / CC0 records
qualify. Unknown licenses, author-manuscript-only licenses and OA flags do not.
"""
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import re
import time
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

HOST = 'sci-hub.ru'
SOURCE = 'scihub_oa'


def license_evidence(meta):
    if meta.get('metadata_source') != 'Crossref': return None
    entries=meta.get('licenses', [])
    if not isinstance(entries,list) or not meta.get('doi'): return None
    for entry in entries:
        if not isinstance(entry,dict) or entry.get('content-version') != 'vor': continue
        url = entry.get('URL','')
        if not isinstance(url,str): continue
        try: p = urlsplit(url)
        except ValueError: continue
        if (p.scheme not in ('http','https') or p.netloc != 'creativecommons.org' or p.query or p.fragment): continue
        if not re.fullmatch(r'/(?:licenses/by(?:-sa)?/(?:1\.0|2\.0|2\.5|3\.0|4\.0)|publicdomain/zero/1\.0)/?',p.path): continue
        try:
            parts = entry['start']['date-parts'][0]
            if len(parts) != 3: continue
            start = datetime(*parts, tzinfo=timezone.utc)
            if start > datetime.now(timezone.utc): continue
        except (KeyError, TypeError, ValueError, IndexError): continue
        return dict(provider='Crossref', doi=meta['doi'], license_url=url,
                    content_version='vor', effective_date=start.date().isoformat())
    return None


class PDFLinks(HTMLParser):
    def __init__(self):
        super().__init__(); self.urls=[]
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag in ('iframe','embed') and a.get('src'): self.urls.append(a['src'])
        if tag=='object' and a.get('data'): self.urls.append(a['data'])
        if tag=='a' and a.get('href'):
            try:
                if urlsplit(a['href']).path.lower().endswith('.pdf'): self.urls.append(a['href'])
            except ValueError: pass


def acquire(engine, meta, deadline):
    from free_sources import public_url, source_url
    from pipeline import validate_pdf
    start=time.monotonic(); events=[]
    def event(status, **extra): events.append(dict(source=SOURCE,status=status,**extra))
    def done(**extra):
        return dict(status='unresolved',events=events,seconds=round(time.monotonic()-start,3),**extra)
    proof=license_evidence(meta)
    if not proof:
        event('open_license_not_verified'); return done()
    if start >= deadline:
        event('deadline'); return done()
    # Cap this supplementary source's whole phase without extending the batch budget.
    deadline=min(deadline,start+25)
    event('open_license_verified',license_url=proof['license_url'])
    root=engine.root/SOURCE/hashlib.sha256(meta['doi'].encode()).hexdigest()[:20]
    root.mkdir(parents=True,exist_ok=True)
    def success(path, check, origin, cached=False):
        return dict(status='verified',pdf=str(path),validation=check,source=SOURCE,
                    source_url=origin,source_version='unknown',license_evidence=proof,
                    events=events,seconds=round(time.monotonic()-start,3),cache_hit=cached)
    for path in sorted(root.glob('*.pdf')):
        try:
            info=json.loads(path.with_suffix('.json').read_text())
            source_url(info['source_url'],{HOST})
            check=validate_pdf(path,meta)
            if check['status']=='verified' and check['sha256']==info['sha256']:
                event('cache_verified'); return success(path,check,info['source_url'],True)
        except (OSError,ValueError,KeyError): continue
    url='https://'+HOST+'/'+quote(meta['doi'],safe='/')
    result=engine.fetch(SOURCE,url,{},deadline,events)
    if not result: return done()
    data,final=result
    if not data.startswith(b'%PDF-'):
        html=data.decode('utf-8','replace')
        if any(marker in html.lower() for marker in (
                'cf-chl-', 'g-recaptcha', 'hcaptcha', 'name="captcha"', "name='captcha'", 'just a moment')):
            with engine.lock: engine.challenge_hosts.add(HOST)
            event('browser_verification_required'); return done()
        parser=PDFLinks(); parser.feed(html)
        urls=[]
        for link in parser.urls:
            try:
                candidate=public_url(urljoin(final,link))
                source_url(candidate,{HOST})
                if candidate not in urls: urls.append(candidate)
            except ValueError:
                event('unsupported_pdf_link'); continue
        if len(urls)!=1:
            event('no_pdf_link' if not urls else 'ambiguous_pdf_links',count=len(urls)); return done()
        result=engine.fetch(SOURCE,urls[0],{},deadline,events,pdf=True)
        if not result: return done()
        data,final=result
    if not data.startswith(b'%PDF-'):
        event('not_pdf'); return done()
    path=root/(hashlib.sha256(data).hexdigest()[:20]+'.pdf')
    path.write_bytes(data)
    check=validate_pdf(path,meta)
    event('validation',result=check['status'],reason=check.get('reason'))
    # This supplementary source requires DOI confirmation, not title-only probable matches.
    if check['status']!='verified': return done()
    p=urlsplit(final); origin=urlunsplit(p._replace(query='',fragment=''))
    info=dict(source_url=origin,sha256=check['sha256'],license_evidence=proof)
    path.with_suffix('.json').write_text(json.dumps(info),encoding='utf-8')
    return success(path,check,origin)
