#!/usr/bin/env python3
"""Resumable metadata -> full Zotero lookup -> OA -> guarded AbleSci pipeline."""
import argparse
import json
import time
import sys

from pipeline import Batch, doi_normalize
from zotero_gui import execute
from ablesci import AbleSci, navigate


def run(b, dois, test_skip_find=False, test_force_gui_attach=False):
    jobs = []
    for doi in dict.fromkeys(doi_normalize(d) for d in dois):
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
            b.event({'doi': doi}, 'metadata_error', error=type(e).__name__)
    missing = [j for j in jobs if j['status'] == 'metadata_ready']
    if missing and not test_skip_find:
        result = execute(b, 'find', missing)
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


def finish_ablesci(b, dois, downloads_dir, per_paper=0, total=0, wait_seconds=45):
    adapter = AbleSci(b.root)
    delayed = []
    for doi in dict.fromkeys(doi_normalize(d) for d in dois):
        row = b.db.execute('SELECT data FROM jobs WHERE doi=?', (doi,)).fetchone()
        if not row:
            continue
        job = json.loads(row[0])
        if job['status'] != 'needs_ablesci':
            continue
        req = adapter.db.execute('SELECT status,url FROM requests WHERE doi=?', (doi,)).fetchone()
        try:
            if not req or req[0] == 'prepared':
                if per_paper < 10 or total < 10:
                    b.event(job, 'ablesci_budget_required')
                    continue
                prepared = adapter.prepare(doi)
                if prepared['status'] != 'prepared':
                    b.event(job, 'ablesci_review', **prepared)
                    continue
                posted = adapter.submit(doi, per_paper, total)
                b.event(job, 'ablesci_post', result=posted)
                if posted['status'] != 'submitted':
                    continue
                req = ('submitted', posted['url'])
            if req[0] != 'submitted' or not req[1]:
                b.event(job, 'ablesci_uncertain_requires_reconcile')
                continue
            deadline = time.monotonic() + max(0, min(wait_seconds, 120))
            while True:
                navigate(req[1])
                state = adapter.reconcile(doi)
                if state['status'] == 'file_available':
                    result = adapter.download(doi, downloads_dir)
                    b.event(job, 'ablesci_download', **result)
                    if result['status'] == 'download_verified':
                        delayed.append(adapter.job(doi))
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    b.event(job, 'ablesci_waiting', url=req[1])
                    break
                time.sleep(min(30, remaining))
        except Exception as e:
            b.event(job, 'ablesci_needs_review', error=str(e))
    if delayed:
        result = execute(b, 'attach', delayed)
        print(json.dumps({'ablesci_attachment': result}, ensure_ascii=False), flush=True)
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
    p.add_argument('--approved-per-paper-points', type=int, default=0)
    p.add_argument('--approved-total-points', type=int, default=0)
    p.add_argument('--ablesci-wait-seconds', type=int, default=45)
    args = p.parse_args()
    if sys.platform != 'darwin':
        p.error('The full desktop pipeline currently requires macOS; no library writes were attempted')
    if args.ablesci and not args.downloads_dir:
        p.error('--ablesci requires explicit --downloads-dir')
    dois = list(args.doi)
    if args.doi_file:
        dois += [x.strip() for x in Path(args.doi_file).read_text(encoding='utf-8').splitlines()
                 if x.strip() and not x.lstrip().startswith('#')]
    b = Batch(args.work_dir, args.collection, args.prefs)
    results = run(b, dois, args.test_skip_find, args.test_force_gui_attach)
    if args.ablesci:
        if not args.downloads_dir:
            p.error('--ablesci requires explicit --downloads-dir')
        results = finish_ablesci(b, dois, args.downloads_dir, args.approved_per_paper_points,
                                args.approved_total_points, args.ablesci_wait_seconds)
    print(json.dumps([{'doi': j['doi'], 'status': j['status']} for j in results], ensure_ascii=False))


if __name__ == '__main__':
    main()
