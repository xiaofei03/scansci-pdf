#!/usr/bin/env python3
"""AbleSci browser adapter for the user's already logged-in Chrome tab.

Uses macOS Chrome Apple Events DOM clicks (not coordinates, requests, cookies,
or private site APIs). Inspect/prepare are non-posting; submit requires explicit
budgets and a previously prepared queue entry. Verified acceptance is opt-in;
rejection is never automatic.
"""
import argparse
import json
import sqlite3
import subprocess
import time
import shutil
import urllib.parse
import re
from pathlib import Path

from pipeline import doi_normalize, norm, validate_pdf


def chrome(js, allow_verified_departure=False):
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
    if allow_verified_departure:
        # Only called after validating the exact job's downloaded PDF. The native
        # prompt cancels an obsolete duplicate transfer, never auth/permission checks.
        process=subprocess.Popen(['osascript','-e',script,js],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        for _ in range(40):
            if process.poll() is not None:break
            time.sleep(0.25)
            subprocess.run(['osascript','-e','''tell application "System Events" to tell process "Google Chrome"
              if exists button "离开" of window 1 then
                if (value of every static text of window 1) contains "离开此网站？" then click button "离开" of window 1
              end if
              end tell'''],capture_output=True,timeout=3)
        stdout,stderr=process.communicate(timeout=10)
        out=subprocess.CompletedProcess(process.args,process.returncode,stdout,stderr)
    else:
        out = subprocess.run(['osascript', '-e', script, js], capture_output=True, text=True, timeout=20)
    if out.returncode:
        raise RuntimeError(out.stderr.strip())
    return json.loads(out.stdout)


def navigate(url, verified_departure=False, refresh=False):
    p = urllib.parse.urlparse(url)
    if p.scheme != 'https' or p.netloc != 'www.ablesci.com':
        raise ValueError('Only observed AbleSci URLs allowed')
    current = transfer_state()
    if current['url'] == url and not refresh:
        return
    if current.get('active') and not verified_departure:
        raise RuntimeError('Download still active; do not navigate away')
    chrome('setTimeout(()=>location.assign(' + json.dumps(url) + '),100); JSON.stringify({navigating:true})',allow_verified_departure=verified_departure)
    for _ in range(15):
        time.sleep(1)
        state = chrome('JSON.stringify({url:location.href,ready:document.readyState})')
        if state['url'] == url and state['ready'] == 'complete':
            return
    raise RuntimeError('Page navigation not confirmed')


def read_chrome(js):
    """Retry empty read-only snapshots during page transitions, never writes."""
    for attempt in range(3):
        try:return chrome(js)
        except json.JSONDecodeError:
            if attempt==2:raise
            time.sleep(0.5)


def snapshot():
    # No hidden fields, cookie values, HTML dump, or authentication data retained.
    return read_chrome('''JSON.stringify({url:location.href,title:document.title,
      text:(Array.from(document.body.innerText.split('实时播报')[0]).slice(0,12000).join('')+'\\n'+
        Array.from(document.querySelectorAll('.layui-layer')).filter(e=>e.getClientRects().length).map(e=>e.innerText).join('\\n')),
      fields:Array.from(document.querySelectorAll('input:not([type=hidden]),textarea,button'))
        .filter(x=>x.type!=='password').map(x=>({id:x.id,name:x.name,type:x.type,text:x.innerText,value:x.value})),
      links:Array.from(document.querySelectorAll('a')).filter(x=>/assist\\/(detail|download)|下载|Download|下一页/.test(x.href+x.innerText))
        .map(x=>({text:x.innerText,url:x.href}))})''')


def guard(state):
    if not state['url'].startswith('https://www.ablesci.com/'):
        raise RuntimeError('Unexpected page origin')
    if any(t in state['text'] for t in ('请先登录', '登录后', '人机验证', '安全验证', '验证码', 'Just a moment')):
        raise RuntimeError('Login/verification requires user action')


def transfer_state():
    """Visible DOM only; no cookies, site internals, or private download endpoints."""
    return read_chrome('''(()=>{const text=document.body.innerText.split('常见问题')[0];
      const downloading=location.pathname==='/assist/download';
      const complete=/下载已完成|下载完成|下载成功|文件已保存|浏览器已发起保存/.test(text);
      const terminal=complete || /浏览器未能继续接收|下载失败/.test(text);
      const m=text.match(/正在下载\\s*([0-9.]+)%/);
      return JSON.stringify({url:location.href,downloading,
        active:downloading && !terminal && /正在下载|正在接收|正在连接/.test(text),
        failed:downloading && /浏览器未能继续接收|下载失败/.test(text),
        complete:downloading && complete,
        percent:m?Number(m[1]):null,
        text:downloading?Array.from(text).slice(-3000).join(''):'',
        buttons:downloading?Array.from(document.querySelectorAll('button'))
          .filter(e=>e.getClientRects().length).map(e=>({id:e.id,text:e.innerText,disabled:e.disabled})):[]});})()''')


class AbleSci:
    def __init__(self, directory):
        self.db = sqlite3.connect(Path(directory) / 'jobs.sqlite')
        self.db.execute('''CREATE TABLE IF NOT EXISTS requests
            (doi TEXT PRIMARY KEY, status TEXT NOT NULL, points INTEGER NOT NULL,
             title TEXT NOT NULL, url TEXT, updated REAL NOT NULL)''')
        # Existing batch recovery ledgers use this schema too. Uncertain reservations
        # count against caps; a failed transfer is not assumed to refund points.
        self.db.execute('CREATE TABLE IF NOT EXISTS speed_budget(doi TEXT PRIMARY KEY, points INTEGER, status TEXT)')

    def save_job(self, job):
        self.db.execute('UPDATE jobs SET data=? WHERE doi=?', (json.dumps(job, ensure_ascii=False),job['doi']))
        self.db.commit()

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
        create_state=snapshot()
        guard(create_state)
        if '请先处理完毕再发起新的求助' in create_state['text']:
            return dict(status='pending_acceptance_blocks_new_requests')
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

    def submit(self, doi, per_paper, total, allow_site_minimum=False):
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
        speed = self.db.execute('SELECT COALESCE(SUM(points),0) FROM speed_budget').fetchone()[0]
        own_speed = self.db.execute('SELECT COALESCE(SUM(points),0) FROM speed_budget WHERE doi=?',(doi,)).fetchone()[0]
        if amount+own_speed > per_paper or spent+speed+amount > total or amount < 10:
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
            if '积分不符合最低要求提醒' in state['text']:
                if not allow_site_minimum:
                    return dict(status='site_minimum_approval_required',doi=doi)
                self.confirm_site_minimum(doi,per_paper,total)
                continue
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

    def confirm_site_minimum(self, doi, per_paper, total):
        import re
        state=snapshot();guard(state)
        if state['url']!='https://www.ablesci.com/assist/create' or '积分不符合最低要求提醒' not in state['text']:
            raise RuntimeError('No site-minimum confirmation')
        fields={x['id']:x.get('value') for x in state['fields'] if x.get('id')}
        if fields.get('Assist-doi')!=doi:raise RuntimeError('Wrong minimum-price paper')
        minimum=int(re.findall(r'最低需要\s*(\d+)\s*积分',state['text'])[-1])
        self.db.execute('BEGIN IMMEDIATE')
        row=self.db.execute('SELECT status,points FROM requests WHERE doi=?',(doi,)).fetchone()
        if not row or row[0]!='posting_uncertain':
            self.db.rollback();raise RuntimeError('Minimum lacks original reservation')
        spent=self.db.execute("SELECT COALESCE(SUM(points),0) FROM requests WHERE status IN ('submitted','posting_uncertain')").fetchone()[0]
        speed=self.db.execute('SELECT COALESCE(SUM(points),0) FROM speed_budget').fetchone()[0]
        own_speed=self.db.execute('SELECT COALESCE(SUM(points),0) FROM speed_budget WHERE doi=?',(doi,)).fetchone()[0]
        if minimum+own_speed>per_paper or spent-row[1]+minimum+speed>total:
            self.db.rollback();raise RuntimeError('Site minimum exceeds authorized budget')
        self.db.execute('UPDATE requests SET points=?,updated=? WHERE doi=?',(minimum,time.time(),doi));self.db.commit()
        chrome('''(()=>{const es=Array.from(document.querySelectorAll('a,button'))
          .filter(e=>e.getClientRects().length&&e.innerText.trim()==='仍然提交');
          if(es.length!==1)throw Error('Minimum confirmation not unique');
          setTimeout(()=>es[0].click(),100);return JSON.stringify({confirmed:true});})()''')

    def recover_submission(self, doi, per_paper, total, allow_site_minimum=False):
        """Observe uncertain outcomes instead of reposting."""
        job=self.job(doi);state=snapshot();guard(state)
        if '积分不符合最低要求提醒' in state['text']:
            if not allow_site_minimum:return dict(status='site_minimum_approval_required')
            self.confirm_site_minimum(doi,per_paper,total)
            time.sleep(2);state=snapshot()
        if '求助发布成功' in state['text'] and '/assist/create' in state['url']:
            chrome('''(()=>{const es=Array.from(document.querySelectorAll('a')).filter(e=>e.innerText.trim()==='查看求助详情');
              if(es.length!==1)throw Error('Missing success detail link');setTimeout(()=>es[0].click(),100);
              return JSON.stringify({opened:true});})()''')
            time.sleep(2);state=snapshot()
        if '/assist/detail?' not in state['url'] or doi not in state['text'].lower():
            navigate('https://www.ablesci.com/my/assist-my',refresh=True)
            state=snapshot();guard(state)
            matches={x['url'] for x in state['links'] if norm(x['text'])==norm(job['metadata']['title'])}
            if len(matches)!=1:return dict(status='posting_uncertain')
            navigate(matches.pop());state=snapshot();guard(state)
        if doi not in state['text'].lower() or norm(job['metadata']['title']) not in norm(state['text']):
            raise RuntimeError('Recovered request identity mismatch')
        if '无人上传【积分已退回】' in state['text']:return dict(status='closed_without_file')
        self.db.execute('UPDATE requests SET status=?,url=?,updated=? WHERE doi=?',('submitted',state['url'],time.time(),doi));self.db.commit()
        return dict(status='submitted',url=state['url'])

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

    def collect_local(self, doi, downloads_dir):
        job = self.job(doi)
        if job.get('source') == 'ablesci' and job.get('pdf'):
            check = validate_pdf(Path(job['pdf']), job['metadata'])
            if check['status'] in ('verified', 'probably_correct'):
                return dict(status='download_verified', pdf=job['pdf'], validation=check)
        root = Path(downloads_dir).resolve()
        if not root.is_dir():
            raise RuntimeError('Browser download directory missing')
        attempt = job.get('download_attempt')
        if not attempt:
            return None
        # Match by paper identity, not by 'exactly one new PDF in Downloads'. Other
        # downloads and preceding batch papers must not stall or contaminate a batch.
        for path in root.glob('*.pdf'):
            if path.stat().st_mtime_ns < attempt['started_ns']:
                continue
            stat = path.stat()
            time.sleep(0.1)
            if (stat.st_size, stat.st_mtime_ns) != (path.stat().st_size,path.stat().st_mtime_ns):
                continue
            check = validate_pdf(path, job['metadata'])
            if check['status'] not in ('verified', 'probably_correct'):
                if check.get('title_match') and check.get('first_author_match'):
                    job['download_review']={'pdf':str(path),'validation':check}
                    self.save_job(job)
                continue
            dest = Path(self.db.execute('PRAGMA database_list').fetchone()[2]).parent / ('ablesci-' + check['sha256'][:20] + '.pdf')
            shutil.copy2(path, dest)
            req=self.db.execute('SELECT url FROM requests WHERE doi=?',(doi,)).fetchone()
            job.update(pdf=str(dest), source='ablesci', source_url=req[0],
                       status='attachment_session_required', validation=check)
            self.save_job(job)
            return dict(status='download_verified', pdf=str(dest), validation=check)
        return None

    def enable_fast(self, doi, per_paper, total):
        state=transfer_state()
        if not state['downloading'] or '高速下载扣 2 积分' not in state['text']:
            raise RuntimeError('Fast-download page/2-point price not confirmed')
        if not any(e['id']=='download-highspeed-direct' and not e['disabled'] for e in state['buttons']):
            return None  # Page is still initializing, not a failed/paid attempt.
        self.db.execute('BEGIN IMMEDIATE')
        prior=self.db.execute('SELECT points,status FROM speed_budget WHERE doi=?',(doi,)).fetchone()
        if prior:
            self.db.rollback()
            # A lost confirmation response is not permission to confirm payment
            # again. Keep the reservation and resume the free/current transfer.
            if prior[1]=='reserved_uncertain':raise RuntimeError('Earlier fast payment uncertain; reconcile before retry')
            return False
        spend=self.db.execute("SELECT COALESCE(SUM(points),0) FROM requests WHERE status IN ('submitted','posting_uncertain')").fetchone()[0]
        speed=self.db.execute('SELECT COALESCE(SUM(points),0) FROM speed_budget').fetchone()[0]
        own=self.db.execute('SELECT points FROM requests WHERE doi=?',(doi,)).fetchone()[0]
        extra=0 if prior else 2
        if per_paper<own+2 or total<spend+speed+extra:
            self.db.rollback();return False
        if not prior:self.db.execute('INSERT INTO speed_budget VALUES(?,?,?)',(doi,2,'reserved_uncertain'))
        self.db.commit()
        chrome('document.getElementById("download-highspeed-direct").click();JSON.stringify({clicked:true})')
        # The modal can arrive asynchronously; never treat a missing immediate
        # confirmation as success. Poll only visible public controls.
        for _ in range(10):
            time.sleep(1)
            modal=chrome('''(()=>{const es=Array.from(document.querySelectorAll('.layui-layer'))
              .filter(e=>e.getClientRects().length&&e.innerText.includes('确认使用高速通道'));
              return JSON.stringify({url:location.href,dialogs:es.map(e=>e.innerText)});})()''')
            if modal['dialogs']:
                if modal['url']!=state['url'] or len(modal['dialogs'])!=1:
                    raise RuntimeError('Fast-download confirmation identity ambiguous')
                text=modal['dialogs'][0]
                prices=re.findall(r'(?:扣除|扣|消耗|支付|花费|需要)\s*(\d+)\s*积分',text)
                if not prices or any(int(p)!=2 for p in prices):
                    raise RuntimeError('Fast-download confirmation price unknown or changed')
                done=chrome('''(()=>{const es=Array.from(document.querySelectorAll('.layui-layer'))
                  .filter(e=>e.getClientRects().length&&e.innerText==='''+json.dumps(text)+''');
                  if(es.length!==1)throw Error('Confirmation changed');
                  const bs=Array.from(es[0].querySelectorAll('a,button'))
                    .filter(e=>e.innerText.trim()==='确认使用高速通道');
                  if(bs.length!==1)throw Error('Confirmation button ambiguous');
                  bs[0].click();return JSON.stringify({confirmed:true});})()''')
            else:done={'confirmed':False}
            if done['confirmed']:break
            current=transfer_state()
            if current['failed'] or current['complete']:break
        self.db.execute('UPDATE speed_budget SET status=? WHERE doi=?',('attempted',doi));self.db.commit()
        return True

    def download(self, doi, downloads_dir, wait_seconds=900, fast=False, per_paper=0, total=0):
        found=self.collect_local(doi, downloads_dir)
        if found:return found
        job=self.job(doi)
        current=transfer_state()
        attempt=job.get('download_attempt')
        if not (attempt and current['url']==attempt['url']):
            if current.get('active'):
                raise RuntimeError('Another transfer is active; resume it before this paper')
            req=self.db.execute('SELECT status,url FROM requests WHERE doi=?',(doi,)).fetchone()
            if not req or req[0]!='submitted' or not req[1]:raise RuntimeError('No confirmed request')
            navigate(req[1])
            result=self.reconcile(doi)
            if len(result['files'])!=1:return dict(status='waiting_or_multiple_files',files=result['files'])
            url=result['files'][0]['url']
            if attempt and attempt['url']!=url:raise RuntimeError('Pending file changed; needs review')
            if not attempt:
                attempt=dict(url=url,started_ns=time.time_ns())
                job['download_attempt']=attempt
            attempt['browser_starts']=attempt.get('browser_starts',0)+1
            if attempt['browser_starts']>4:raise RuntimeError('Download restart limit reached')
            self.save_job(job)
            navigate(url)
        deadline=time.monotonic()+max(1,wait_seconds)
        last_progress=time.monotonic()
        percent=None
        tried=set(job.get('download_routes',[]))
        last_report=0
        while time.monotonic()<deadline:
            found=self.collect_local(doi,downloads_dir)
            if found:return found
            current=transfer_state()
            if current['url']!=attempt['url']:raise RuntimeError('Transfer navigated away; inspect before retry')
            guard(dict(url=current['url'],text=current['text']))
            if current['percent']!=percent:
                percent=current['percent'];last_progress=time.monotonic()
            if current['complete']:
                review=self.job(doi).get('download_review')
                if not review and not job.get('manual_save_attempted'):
                    job['manual_save_attempted']=True;self.save_job(job)
                    try:
                        chrome('''(()=>{const es=Array.from(document.querySelectorAll('a'))
                          .filter(e=>e.getClientRects().length&&e.innerText.trim()==='手动保存文件');
                          if(es.length!==1)throw Error('Manual save control not unique');
                          es[0].click();return JSON.stringify({saved:true});})()''')
                    except json.JSONDecodeError:
                        # Saving can succeed while Chrome returns no JSON. Never
                        # replay this click; reconcile the local PDF instead.
                        pass
                    time.sleep(2)
                    found=self.collect_local(doi,downloads_dir)
                    if found:return found
                return dict(status='download_needs_review' if review else 'download_saved_not_located',
                            review=review,action='Inspect saved file / browser blocked-download permission; do not restart transfer')
            if time.monotonic()-last_report>30:
                print(json.dumps({'doi':doi,'download_percent':percent,'active':current['active']},ensure_ascii=False),flush=True)
                last_report=time.monotonic()
                job['download_progress']={'at':time.time(),'percent':percent,'active':current['active']}
                self.save_job(job)
            if fast and 'fast' not in tried and not current['complete']:
                enabled=self.enable_fast(doi,per_paper,total)
                if enabled:
                    tried.add('fast');job['download_routes']=list(tried);self.save_job(job)
                    last_progress=time.monotonic();continue
                if enabled is False:
                    tried.add('fast')
                    job['download_routes']=list(tried);self.save_job(job)
            if current['failed'] or time.monotonic()-last_progress>120:
                routes=[e for e in current['buttons'] if not e['disabled'] and e['text'].lstrip().startswith('线路')]
                route=next((e for e in routes if e['text'].splitlines()[0] not in tried),None)
                if not route:
                    return dict(status='download_retry_exhausted',percent=percent)
                label=route['text'].splitlines()[0]
                chrome('''(()=>{const label='''+json.dumps(label)+''';const es=Array.from(document.querySelectorAll('button'))
                  .filter(e=>e.getClientRects().length&&!e.disabled&&e.innerText.trim().split('\\n')[0]===label);
                  if(es.length!==1)throw Error('Route not unique');es[0].click();return JSON.stringify({changed:true});})()''')
                tried.add(label);job['download_routes']=list(tried);self.save_job(job)
                last_progress=time.monotonic()
            time.sleep(3)
        return dict(status='download_pending',percent=percent,action='Keep transfer page open and resume same job')

    def accept_verified(self, doi, approved=False):
        if not approved:raise RuntimeError('Acceptance requires explicit batch authorization')
        job=self.job(doi)
        if job.get('status')!='complete':raise RuntimeError('Zotero-managed attachment must be verified before acceptance')
        if not job.get('pdf'):raise RuntimeError('No verified downloaded file to accept')
        check=validate_pdf(Path(job['pdf']),job['metadata'])
        if check['status'] not in ('verified','probably_correct'):raise RuntimeError('Refusing unverified acceptance')
        req=self.db.execute('SELECT url FROM requests WHERE doi=?',(doi,)).fetchone()
        if not req or not req[0]:raise RuntimeError('Missing exact request')
        # A successful acceptance may leave a success modal over stale detail text.
        # Refresh the exact recorded detail before deciding whether another click is needed.
        navigate(req[0],verified_departure=True,refresh=True);state=snapshot();guard(state)
        if doi not in state['text'].lower() or norm(job['metadata']['title']) not in norm(state['text']):
            raise RuntimeError('Wrong acceptance identity')
        if '已采纳' in state['text'] and '已完结' in state['text']:
            job['ablesci_accepted']=True;self.save_job(job);return dict(status='already_accepted')
        chrome('''(()=>{const es=Array.from(document.querySelectorAll('a,button')).filter(e=>e.getClientRects().length&&e.innerText.trim()==='确定');
          if(es.length===1&&document.body.innerText.includes('恭喜您，已经有人上传了文件'))es[0].click();
          const bs=Array.from(document.querySelectorAll('button')).filter(e=>e.getClientRects().length&&e.innerText.trim()==='采纳文件');
          if(bs.length!==1)throw Error('Acceptance control not unique');bs[0].click();return JSON.stringify({clicked:true});})()''')
        time.sleep(1)
        job['acceptance_status']='confirming';self.save_job(job)
        chrome('''(()=>{const dialogs=Array.from(document.querySelectorAll('.layui-layer'))
          .filter(e=>e.getClientRects().length&&e.innerText.includes('确认接受应助吗？'));
          if(dialogs.length!==1)throw Error('Confirmation dialog not unique');
          const e=dialogs[0].querySelector('.layui-layer-btn0');if(!e)throw Error('No confirmation button');
          setTimeout(()=>e.click(),100);return JSON.stringify({confirmed:true});})()''')
        time.sleep(2)
        chrome('''(()=>{const dialogs=Array.from(document.querySelectorAll('.layui-layer'))
          .filter(e=>e.getClientRects().length&&e.innerText.includes('操作成功，感谢使用科研通'));
          if(dialogs.length===1){const b=dialogs[0].querySelector('.layui-layer-btn0');if(b)setTimeout(()=>b.click(),100);}
          return JSON.stringify({successDialog:dialogs.length});})()''')
        time.sleep(1)
        navigate(req[0],refresh=True)
        state=snapshot()
        if '已采纳' not in state['text'] or '已完结' not in state['text']:
            raise RuntimeError('Acceptance pending; reconcile before retry')
        job['ablesci_accepted']=True;self.save_job(job)
        return dict(status='accepted_verified')


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
