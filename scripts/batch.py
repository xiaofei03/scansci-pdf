#!/usr/bin/env python3
"""Resumable metadata -> full Zotero lookup -> OA -> guarded AbleSci pipeline."""
import argparse
import json
import time
import sys
from pathlib import Path

from pipeline import Batch, doi_normalize
from zotero_gui import execute, reconcile, close_own_window, close_stream
from ablesci import AbleSci, navigate, transfer_state
from doctor import download_preflight


def requested_complete(results, dois, accept=False):
    states={j['doi']:j for j in results}
    requested={doi_normalize(d) for d in dois}
    return bool(requested) and all(states.get(d,{}).get('status')=='complete'
        and (not accept or states[d].get('source')!='ablesci' or states[d].get('ablesci_accepted'))
        for d in requested)


def attachment_complete(adapter, doi):
    return adapter.job(doi).get('status')=='complete'


def completion_exit_code(results, dois, accept=False, needs_review=False):
    if requested_complete(results, dois, accept):
        return 0
    return 2 if needs_review else 3


def resume_gui(b, dois):
    """Reconcile the same handle before any repeated library write."""
    jobs=[json.loads(row[0]) for d in dois for row in b.db.execute(
        'SELECT data FROM jobs WHERE doi=?',(doi_normalize(d),))]
    for report in {j.get('gui_report') for j in jobs if j.get('gui_launch_status')=='launching'}:
        if not report or not Path(report).exists():return False
        try:state=json.loads(Path(report).read_text(encoding='utf-8'))
        except json.JSONDecodeError:return False
        if state.get('status')=='running':return False
        affected=[j for j in jobs if j.get('gui_report')==report]
        reconcile(b,affected,state)
        close_stream(report)
        close_own_window(state['run_id'])
    return True


def run(b, dois, test_skip_find=False, test_force_gui_attach=False):
    if not resume_gui(b,dois):return b.report()
    jobs = []
    for doi in dict.fromkeys(doi_normalize(d) for d in dois):
        prior={}
        try:
            row = b.db.execute('SELECT data FROM jobs WHERE doi=?', (doi,)).fetchone()
            prior = json.loads(row[0]) if row else {}
            # Keep verified negative lookups/download handles across repeated runs.
            if prior.get('status') in ('needs_ablesci', 'attachment_session_required',
                                       'gui_result_needs_review', 'resolver_pending'):
                jobs.append(prior)
            else:
                jobs.append(b.run_one(doi, metadata_only=True))
        except Exception as e:
            # The attempted operation may already have persisted its uncertainty
            # marker/session. Never replace that newer checkpoint with stale prior.
            latest=b.db.execute('SELECT data FROM jobs WHERE doi=?',(doi,)).fetchone()
            failed=json.loads(latest[0]) if latest else (prior if prior else {'doi':doi})
            failed.update(status='metadata_error',error_type=type(e).__name__)
            b.save(failed)
            b.event({'doi': doi}, 'metadata_error', error=type(e).__name__)
    missing = [j for j in jobs if j['status'] == 'metadata_ready']
    unresolved=[j for j in missing if not j.get('full_lookup_complete')]
    if unresolved and not test_skip_find:
        result = execute(b, 'find', unresolved)
        print(json.dumps({'zotero_full_lookup': result}, ensure_ascii=False), flush=True)
        if result['status'] != 'complete':
            return b.report()
    if test_skip_find:
        b.event({'doi': ''}, 'test_mode', full_zotero='intentionally_skipped')
    b.zotero_oa = False  # Full lookup already included OA; do not repeat it.
    delayed = [j for j in jobs if j['status'] == 'attachment_session_required' and j.get('pdf')]
    for original in missing:
        row = b.db.execute('SELECT data FROM jobs WHERE doi=?', (original['doi'],)).fetchone()
        job = json.loads(row[0])
        if job.get('status') == 'complete':
            continue
        if not test_skip_find and not job.get('full_lookup_complete'):
            continue  # An errored or uncertain full lookup is not a clean miss.
        if test_force_gui_attach:
            # Simulate loss of the import session without deleting/recreating the item.
            job.pop('session', None)
            b.save(job)
            b.event(job, 'test_mode', connector_session='intentionally_unavailable')
        try:
            job = b.run_one(job['doi'])
            if job['status'] == 'attachment_session_required' and job.get('pdf'):
                delayed.append(job)
        except Exception as e:
            b.event(job, 'fallback_error', error=type(e).__name__)
    if delayed:
        result = execute(b, 'attach', delayed)
        print(json.dumps({'delayed_attachment': result}, ensure_ascii=False), flush=True)
    return b.report()


def finish_ablesci(b, dois, downloads_dir, per_paper=0, total=0, wait_seconds=45,
                   download_wait=900, fast=False, accept=False, paper_budgets=None, allow_site_minimum=False):
    adapter = AbleSci(b.root)
    b.needs_review=False
    if not resume_gui(b,dois):return b.report()
    if requested_complete(b.report(),dois,accept):return b.report()
    # Resolve existing requests/transfers before creating another. This also respects
    # the site's low-balance rule that uploaded files must be handled first.
    queue=list(dict.fromkeys(doi_normalize(d) for d in dois))
    current=transfer_state()
    def priority(d):
        try:job=adapter.job(d)
        except RuntimeError:return 3
        if job.get('download_attempt',{}).get('url')==current['url']:return 0
        if job.get('pdf') and job.get('source')=='ablesci':return 1
        return 2 if adapter.db.execute('SELECT 1 FROM requests WHERE doi=?',(d,)).fetchone() else 3
    queue.sort(key=priority)
    for doi in queue:
        limit=(paper_budgets or {}).get(doi,per_paper)
        minimum_authorized=allow_site_minimum and doi in (paper_budgets or {})
        row = b.db.execute('SELECT data FROM jobs WHERE doi=?', (doi,)).fetchone()
        if not row:
            continue
        job = json.loads(row[0])
        req = adapter.db.execute('SELECT status,url FROM requests WHERE doi=?', (doi,)).fetchone()
        try:
            if req and req[0]=='posting_uncertain':
                recovered=adapter.recover_submission(doi,limit,total,minimum_authorized)
                b.event(job,'ablesci_recovered_submission',result=recovered)
                if recovered['status']!='submitted':
                    b.needs_review=True;return b.report()
                req=('submitted',recovered['url'])
            if job.get('source')=='ablesci' and job.get('pdf'):
                if job['status']!='complete':
                    result=execute(b,'attach',[job])
                    if result['status']!='complete' or not attachment_complete(adapter,doi):return b.report()
                if accept and not job.get('ablesci_accepted'):
                    b.event(job,'ablesci_acceptance',result=adapter.accept_verified(doi,approved=True))
                continue
            if job['status'] != 'needs_ablesci':continue
            # download() can resume a live transfer or pick up a finished local file
            # without first navigating away from its page.
            if req and req[0]=='submitted' and job.get('download_attempt'):
                result=adapter.download(doi,downloads_dir,download_wait,fast,limit,total)
                b.event(job,'ablesci_download',**result)
                if result['status']!='download_verified':
                    b.needs_review=result['status']!='download_pending'
                    return b.report()
                job=adapter.job(doi)
                attached=execute(b,'attach',[job])
                if attached['status']!='complete' or not attachment_complete(adapter,doi):return b.report()
                if accept:b.event(job,'ablesci_acceptance',result=adapter.accept_verified(doi,approved=True))
                continue
            if not req or req[0] in ('prepared','minimum_approval_required'):
                if limit < 10 or total < 10:
                    b.event(job, 'ablesci_budget_required')
                    continue
                latest=adapter.db.execute("SELECT MAX(updated) FROM requests WHERE status IN ('submitted','posting_uncertain')").fetchone()[0]
                if latest:time.sleep(max(0,31-(time.time()-latest)))
                if job.get('site_minimum_required') and not minimum_authorized:
                    b.needs_review=True
                    b.event(job,'site_minimum_approval_required',minimum=job['site_minimum_required'])
                    return b.report()
                reward=job.get('site_minimum_required',10)
                if reward>limit:
                    b.needs_review=True
                    b.event(job,'site_minimum_exceeds_cap',minimum=reward,cap=limit)
                    return b.report()
                prepared = adapter.prepare(doi,points=reward)
                if prepared['status'] != 'prepared':
                    b.needs_review=prepared['status']!='pending_acceptance_blocks_new_requests'
                    b.event(job, 'ablesci_review', **prepared)
                    return b.report()
                posted = adapter.submit(doi, limit, total,minimum_authorized)
                b.event(job, 'ablesci_post', result=posted)
                if posted['status'] != 'submitted':
                    return b.report()
                req = ('submitted', posted['url'])
            if req[0] != 'submitted' or not req[1]:
                b.event(job, 'ablesci_uncertain_requires_reconcile')
                return b.report()
            deadline = time.monotonic() + max(0, min(wait_seconds, 120))
            while True:
                # Request pages are server-rendered; rereading the same DOM cannot
                # discover a later upload. This is not the active transfer page.
                navigate(req[1], refresh=True)
                state = adapter.reconcile(doi)
                if state['status'] == 'file_available':
                    result = adapter.download(doi, downloads_dir,download_wait,fast,limit,total)
                    b.event(job, 'ablesci_download', **result)
                    if result['status'] == 'download_verified':
                        job=adapter.job(doi)
                        attached=execute(b,'attach',[job])
                        if attached['status']!='complete' or not attachment_complete(adapter,doi):return b.report()
                        if accept:b.event(job,'ablesci_acceptance',result=adapter.accept_verified(doi,approved=True))
                    else:
                        b.needs_review=result['status']!='download_pending'
                        return b.report()  # Never leave an active transfer to post the next request.
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    b.event(job, 'ablesci_waiting', url=req[1])
                    break
                time.sleep(min(30, remaining))
        except Exception as e:
            b.needs_review=True
            b.event(job, 'ablesci_needs_review', error=str(e))
            return b.report()  # Browser-wide failures must not cascade across every DOI.
    return b.report()


def main():
    from pathlib import Path
    p = argparse.ArgumentParser()
    p.add_argument('--collection', required=True)
    p.add_argument('--work-dir', required=True)
    p.add_argument('--prefs', required=True)
    p.add_argument('--doi', action='append', default=[])
    p.add_argument('--doi-file')
    p.add_argument('--test-skip-find', action='store_true')
    p.add_argument('--test-force-gui-attach', action='store_true')
    p.add_argument('--ablesci', action='store_true')
    p.add_argument('--downloads-dir')
    p.add_argument('--chrome-preferences', help='Preferences file of the Chrome profile owning the AbleSci tab; read-only')
    p.add_argument('--preflight-only', action='store_true', help='Check download setup without library/browser writes or spending')
    p.add_argument('--approved-per-paper-points', type=int, default=0)
    p.add_argument('--approved-total-points', type=int, default=0)
    p.add_argument('--ablesci-wait-seconds', type=int, default=45)
    p.add_argument('--download-wait-seconds',type=int,default=900)
    p.add_argument('--approved-fast-download',action='store_true',default=True,help='Default: prefer 2-point fast downloads within authorized caps')
    p.add_argument('--no-fast-download',dest='approved_fast_download',action='store_false',help='Use free routes only')
    p.add_argument('--approved-accept-verified',action='store_true',help='Authorize acceptance only after PDF validation')
    p.add_argument('--until-complete',action='store_true',help='Keep the same live process polling this batch; no scheduler installation')
    p.add_argument('--max-run-seconds',type=int,default=3600)
    p.add_argument('--paper-budget',action='append',default=[],metavar='DOI=POINTS',help='Explicit per-paper total cap override')
    p.add_argument('--approved-site-minimum',action='store_true',help='Allow mandatory minimum only for explicit --paper-budget DOI overrides')
    args = p.parse_args()
    paper_budgets={}
    for override in args.paper_budget:
        doi,value=override.rsplit('=',1)
        paper_budgets[doi_normalize(doi)]=int(value)
    if sys.platform != 'darwin':
        p.error('The full desktop pipeline currently requires macOS; no library writes were attempted')
    if args.ablesci and not args.downloads_dir:
        p.error('--ablesci requires explicit --downloads-dir')
    if args.ablesci or args.preflight_only:
        readiness = download_preflight(args.chrome_preferences, args.downloads_dir)
        if args.preflight_only or not readiness['ready']:
            print(json.dumps({'stage':'download_preflight', **readiness}, ensure_ascii=False))
            return 0 if readiness['ready'] else 2
    dois = list(args.doi)
    if args.doi_file:
        dois += [x.strip() for x in Path(args.doi_file).read_text(encoding='utf-8').splitlines()
                 if x.strip() and not x.lstrip().startswith('#')]
    b = Batch(args.work_dir, args.collection, args.prefs)
    # Kernel lock, not a stale PID sentinel. Lost observation never authorizes restart.
    import fcntl
    lock=open(b.root/'runner.lock','a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:p.error('Another runner owns this batch; inspect it instead of starting another')
    started=time.monotonic()
    results = run(b, dois, args.test_skip_find, args.test_force_gui_attach)
    if args.ablesci:
        if not args.downloads_dir:
            p.error('--ablesci requires explicit --downloads-dir')
        results = finish_ablesci(b, dois, args.downloads_dir, args.approved_per_paper_points,
                                args.approved_total_points, args.ablesci_wait_seconds,
                                args.download_wait_seconds,args.approved_fast_download,args.approved_accept_verified,paper_budgets,args.approved_site_minimum)
        while args.until_complete and not getattr(b,'needs_review',False) and not requested_complete(results,dois,args.approved_accept_verified) and time.monotonic()-started<args.max_run_seconds:
            print(json.dumps({'batch_waiting':True,'complete':sum(j['status']=='complete' for j in results),'total':len(results)}),flush=True)
            time.sleep(30)
            unresolved=[j['doi'] for j in results if j['doi'] in {doi_normalize(d) for d in dois}
                        and j['status'] not in ('complete','needs_ablesci')]
            if unresolved:results=run(b,unresolved,args.test_skip_find,args.test_force_gui_attach)
            results=finish_ablesci(b,dois,args.downloads_dir,args.approved_per_paper_points,args.approved_total_points,
                                  args.ablesci_wait_seconds,args.download_wait_seconds,args.approved_fast_download,args.approved_accept_verified,paper_budgets,args.approved_site_minimum)
    print(json.dumps([{'doi': j['doi'], 'status': j['status']} for j in results], ensure_ascii=False))
    return completion_exit_code(results, dois, args.approved_accept_verified, getattr(b, 'needs_review', False))


if __name__ == '__main__':
    sys.exit(main())
