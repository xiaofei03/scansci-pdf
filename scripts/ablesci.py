#!/usr/bin/env python3
"""AbleSci browser adapter for the user's already logged-in Chrome tab.

Uses macOS Chrome Apple Events DOM clicks (not coordinates, requests, cookies,
or private site APIs). Inspect/prepare are non-posting; submit requires explicit
budgets and a previously prepared queue entry. No automatic acceptance/rejection.
"""
import argparse
import json
import sqlite3
import subprocess
import time
import shutil
import urllib.parse
from pathlib import Path

from pipeline import doi_normalize, norm, validate_pdf


def chrome(js):
    # Pin to the unique AbleSci tab, not whichever unrelated tab is foreground.
    script = '''on run argv
tell application "Google Chrome"
set foundTabs to {}
repeat with w in windows
repeat with t in tabs of w
if URL of t starts with "https://www.ablesci.com/" then set end of foundTabs to t
end repeat
end repeat
if (count of foundTabs) is not 1 then error "Need exactly one AbleSci tab"
return execute (item 1 of foundTabs) javascript (item 1 of argv)
end tell
end run'''
    out = subprocess.run(['osascript', '-e', script, js], capture_output=True, text=True, timeout=20)
    if out.returncode:
        raise RuntimeError(out.stderr.strip())
    return json.loads(out.stdout)


def navigate(url):
    p = urllib.parse.urlparse(url)
    if p.scheme != 'https' or p.netloc != 'www.ablesci.com':
        raise ValueError('Only observed AbleSci URLs allowed')
    chrome('location.href=' + json.dumps(url) + '; JSON.stringify({navigating:true})')
    for _ in range(15):
        time.sleep(1)
        state = chrome('JSON.stringify({url:location.href,ready:document.readyState})')
        if state['url'] == url and state['ready'] == 'complete':
            return
    raise RuntimeError('Page navigation not confirmed')


def snapshot():
    # No hidden fields, cookie values, HTML dump, or authentication data retained.
    return chrome('''JSON.stringify({url:location.href,title:document.title,
      text:document.body.innerText.split('实时播报')[0].slice(0,12000),
      fields:Array.from(document.querySelectorAll('input:not([type=hidden]),textarea,button'))
        .filter(x=>x.type!=='password').map(x=>({id:x.id,name:x.name,type:x.type,text:x.innerText,value:x.value})),
      links:Array.from(document.querySelectorAll('a')).filter(x=>/assist\\/(detail|download)|下载|Download|下一页/.test(x.href+x.innerText))
        .map(x=>({text:x.innerText,url:x.href}))})''')


def guard(state):
    if not state['url'].startswith('https://www.ablesci.com/'):
        raise RuntimeError('Unexpected page origin')
    if any(t in state['text'] for t in ('请先登录', '登录后', '人机验证', '安全验证', '验证码', 'Just a moment')):
        raise RuntimeError('Login/verification requires user action')


class AbleSci:
    def __init__(self, directory):
        self.db = sqlite3.connect(Path(directory) / 'jobs.sqlite')
        self.db.execute('''CREATE TABLE IF NOT EXISTS requests
            (doi TEXT PRIMARY KEY, status TEXT NOT NULL, points INTEGER NOT NULL,
             title TEXT NOT NULL, url TEXT, updated REAL NOT NULL)''')

    def job(self, doi):
        row = self.db.execute('SELECT data FROM jobs WHERE doi=?', (doi,)).fetchone()
        if not row:
            raise RuntimeError('DOI must have verified metadata in the download queue first')
        return json.loads(row[0])

    def prepare(self, doi, points=10, test_only=False, approved_closed_url=None):
        job = self.job(doi)
        meta = job['metadata']
        if not test_only and job['status'] != 'needs_ablesci':
            raise RuntimeError('Only exhausted-download jobs may be submitted to AbleSci')
        row = self.db.execute('SELECT status FROM requests WHERE doi=?', (doi,)).fetchone()
        if row and row[0] in ('posting_uncertain', 'submitted'):
            raise RuntimeError('Existing or uncertain submission: reconcile, never repost')
        navigate('https://www.ablesci.com/my/assist-my')
        state = snapshot()
        guard(state)
        for link in state['links']:
            if norm(link['text']) == norm(meta['title']):
                if link['url'] != approved_closed_url:
                    return dict(status='existing_request_needs_review', url=link['url'])
                navigate(link['url'])
                detail = snapshot()
                guard(detail)
                if (doi.lower() not in detail['text'].lower()
                        or '已关闭' not in detail['text']
                        or '无人上传【积分已退回】' not in detail['text']):
                    raise RuntimeError('Prior request is not confirmed closed without a file')
        if any('下一页' in l['text'] for l in state['links']):
            raise RuntimeError('Request history paginated; finish duplicate check before posting')
        navigate('https://www.ablesci.com/assist/create')
        guard(snapshot())
        fields = {'Assist-doi': doi, 'Assist-title': meta['title'], 'Assist-url': meta['url'],
                  'Assist-point': str(points),
                  'Assist-note': '; '.join([meta['journal'], meta['year'], ', '.join(meta['authors'])])}
        result = chrome('''(()=>{const fields=''' + json.dumps(fields) + ''';
          if(location.pathname!=='/assist/create')throw Error('wrong page');
          for(const [id,value] of Object.entries(fields)){
            const e=document.getElementById(id); if(!e)throw Error('missing field '+id);
            e.value=value;e.dispatchEvent(new Event('input',{bubbles:true}));
            e.dispatchEvent(new Event('change',{bubbles:true}));
          }
          return JSON.stringify(Object.fromEntries(Object.keys(fields).map(id=>[id,document.getElementById(id).value])));
        })()''')
        if result != fields:
            raise RuntimeError('Filled form does not match queue metadata')
        state = 'test_prepared_no_submit' if test_only else 'prepared'
        self.db.execute('INSERT OR REPLACE INTO requests VALUES (?,?,?,?,?,?)',
                        (doi, state, points, meta['title'], None, time.time()))
        self.db.commit()
        return dict(status=state, doi=doi, title=meta['title'], points=points)

    def submit(self, doi, per_paper, total):
        if per_paper < 10 or total < 10:
            raise RuntimeError('Point spending not authorized: both explicit budgets are required')
        job = self.job(doi)
        if job['status'] != 'needs_ablesci':
            raise RuntimeError('Not an exhausted-download job')
        state = snapshot()
        guard(state)
        row = self.db.execute('SELECT status,points,title FROM requests WHERE doi=?', (doi,)).fetchone()
        if not row or row[0] != 'prepared':
            raise RuntimeError('Prepare and inspect the exact request before submission')
        amount = row[1]
        fields = {x['id']: x.get('value') for x in state['fields'] if x.get('id')}
        if (fields.get('Assist-doi') != doi or fields.get('Assist-title') != row[2]
                or fields.get('Assist-point') != str(amount) or fields.get('Assist-url') != job['metadata']['url']):
            raise RuntimeError('Form changed since preparation')
        self.db.execute('BEGIN IMMEDIATE')
        current = self.db.execute('SELECT status FROM requests WHERE doi=?', (doi,)).fetchone()
        if not current or current[0] != 'prepared':
            self.db.rollback()
            raise RuntimeError('Another worker already reserved this request')
        latest = self.db.execute("SELECT MAX(updated) FROM requests WHERE status IN ('submitted','posting_uncertain')").fetchone()[0]
        if latest and time.time() - latest < 30:
            self.db.rollback()
            raise RuntimeError('Rate guard: wait at least 30 seconds between submissions')
        spent = self.db.execute("SELECT COALESCE(SUM(points),0) FROM requests WHERE status IN ('submitted','posting_uncertain')").fetchone()[0]
        if amount > per_paper or spent + amount > total or amount < 10:
            self.db.rollback()
            raise RuntimeError('Explicit point budget exceeded or below site minimum')
        # Reserve before click: process/network failure never results in a duplicate retry.
        self.db.execute("UPDATE requests SET status='posting_uncertain',updated=? WHERE doi=?", (time.time(), doi))
        self.db.commit()
        chrome('''(()=>{if(location.pathname!=='/assist/create')throw Error('wrong page');
          const b=document.getElementById('form-submit-btn');
          if(!b||b.disabled)throw Error('submit unavailable');
          b.click();return JSON.stringify({clicked:true});})()''')
        for _ in range(15):
            time.sleep(1)
            state = snapshot()
            guard(state)
            if '求助发布成功' in state['text'] and '/assist/create' in state['url']:
                chrome('''(()=>{const a=Array.from(document.querySelectorAll('a')).filter(e=>e.innerText==='查看求助详情');
                  if(a.length!==1)throw Error('Success link not unique');
                  a[0].click();return JSON.stringify({opened:true});})()''')
                continue
            if '/assist/detail?' in state['url'] and doi.lower() in state['text'].lower():
                self.db.execute("UPDATE requests SET status='submitted',url=?,updated=? WHERE doi=?", (state['url'], time.time(), doi))
                self.db.commit()
                return dict(status='submitted', doi=doi, url=state['url'], points=amount)
        return dict(status='posting_uncertain', doi=doi, action='inspect before any retry')

    def reconcile(self, doi):
        state = snapshot()
        guard(state)
        job = self.job(doi)
        if ('/assist/detail?' not in state['url'] or doi not in state['text'].lower()
                or norm(job['metadata']['title']) not in norm(state['text'])):
            raise RuntimeError('Detail page does not match exact queued paper')
        row = self.db.execute('SELECT status,url FROM requests WHERE doi=?', (doi,)).fetchone()
        if not row or row[0] not in ('submitted', 'posting_uncertain'):
            raise RuntimeError('No recorded live submission to reconcile')
        if row[1] and state['url'] != row[1]:
            raise RuntimeError('Wrong request URL; refusing to reconcile a different request')
        if '无人上传【积分已退回】' in state['text']:
            return dict(status='closed_without_file', url=state['url'], files=[])
        self.db.execute("UPDATE requests SET status='submitted',url=?,updated=? WHERE doi=?", (state['url'], time.time(), doi))
        self.db.commit()
        links = [l for l in state['links'] if '/assist/download?' in l['url']]
        return dict(status='file_available' if links else 'waiting', url=state['url'], files=links)

    def download(self, doi, downloads_dir):
        job = self.job(doi)
        if job.get('source') == 'ablesci' and job.get('pdf'):
            check = validate_pdf(Path(job['pdf']), job['metadata'])
            if check['status'] in ('verified', 'probably_correct'):
                return dict(status='download_verified', pdf=job['pdf'], validation=check)
        result = self.reconcile(doi)
        if len(result['files']) != 1:
            return dict(status='waiting_or_multiple_files', files=result['files'])
        job = self.job(doi)
        root = Path(downloads_dir).resolve()
        if not root.is_dir():
            raise RuntimeError('Browser download directory missing')
        url = result['files'][0]['url']
        attempt = job.get('download_attempt')
        if attempt and attempt['url'] != url:
            raise RuntimeError('Returned file changed during a pending download; review first')
        if not attempt:
            attempt = dict(url=url, started_ns=time.time_ns())
            job['download_attempt'] = attempt
            self.db.execute('UPDATE jobs SET data=? WHERE doi=?', (json.dumps(job, ensure_ascii=False), doi))
            self.db.commit()
            chrome('''(()=>{const a=Array.from(document.querySelectorAll('a')).filter(e=>e.href===''' + json.dumps(url) + ''');
              if(a.length!==1)throw Error('Download link not unique');location.assign(a[0].href);return JSON.stringify({clicked:true});})()''')
        previous = None
        for _ in range(45):
            time.sleep(1)
            candidates = [p for p in root.glob('*.pdf')
                          if p.stat().st_mtime_ns >= attempt['started_ns']]
            if len(candidates) != 1:
                continue
            path = candidates[0]
            current = (str(path), path.stat().st_size, path.stat().st_mtime_ns)
            if current != previous:
                previous = current
                continue
            check = validate_pdf(path, job['metadata'])
            if check['status'] not in ('verified', 'probably_correct'):
                return dict(status='download_needs_review', validation=check)
            dest = Path(self.db.execute('PRAGMA database_list').fetchone()[2]).parent / ('ablesci-' + check['sha256'][:20] + '.pdf')
            shutil.copy2(path, dest)
            job.update(pdf=str(dest), source='ablesci', source_url=result['url'],
                       status='attachment_session_required', validation=check)
            self.db.execute('UPDATE jobs SET data=? WHERE doi=?', (json.dumps(job, ensure_ascii=False), doi))
            self.db.commit()
            return dict(status='download_verified', pdf=str(dest), validation=check)
        return dict(status='download_pending', action='Check browser download; do not click again blindly')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['inspect', 'prepare', 'submit', 'reconcile', 'download'])
    p.add_argument('--work-dir', required=True)
    p.add_argument('--doi')
    p.add_argument('--points', type=int, default=10)
    p.add_argument('--approved-per-paper-points', type=int, default=0)
    p.add_argument('--approved-total-points', type=int, default=0)
    p.add_argument('--test-only', action='store_true')
    p.add_argument('--approved-closed-request-url')
    p.add_argument('--downloads-dir')
    args = p.parse_args()
    adapter = AbleSci(args.work_dir)
    if args.action == 'inspect':
        result = snapshot()
    elif args.action == 'prepare':
        result = adapter.prepare(doi_normalize(args.doi), args.points, args.test_only, args.approved_closed_request_url)
    elif args.action == 'reconcile':
        result = adapter.reconcile(doi_normalize(args.doi))
    elif args.action == 'download':
        result = adapter.download(doi_normalize(args.doi), args.downloads_dir)
    else:
        result = adapter.submit(doi_normalize(args.doi), args.approved_per_paper_points, args.approved_total_points)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
