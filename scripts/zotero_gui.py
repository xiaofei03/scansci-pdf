#!/usr/bin/env python3
"""A single bounded Zotero UI launch per batch; no permanent JS bridge.

Uses only generated, collection-scoped commands in Run JavaScript. The clipboard
is replaced by the generated script; it never captures/saves the prior clipboard.
Reports remain under the job work directory. All parent/attachment writes are
performed by Zotero, never via SQLite. macOS and an unlocked screen are required.
"""
import argparse
import json
import subprocess
import time
import uuid
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pipeline import Batch, doi_normalize, managed_pdfs, good, validate_pdf


def close_own_window(run_id):
    script = '''on run argv
tell application "System Events" to tell process "Zotero"
if name of front window does not contain "JavaScript" then return "not_front"
set allElements to entire contents of front window
repeat with e in allElements
try
if role of e is "AXTextArea" and value of e contains (item 1 of argv) then
tell application "Zotero" to activate
delay 0.3
keystroke "w" using command down
return "closed_own_window"
end if
end try
end repeat
return "identity_not_confirmed"
end tell
end run'''
    return subprocess.run(['osascript', '-e', script, run_id], capture_output=True, text=True, timeout=15).stdout.strip()


def execute(batch, action, jobs, wait_seconds=50):
    if action != 'attach':
        return _execute(batch, action, jobs, wait_seconds)
    # Serve only prevalidated bytes on unpredictable loopback URLs, never a directory
    # or a privileged command endpoint. Avoid synchronous importFromFile/getxattr.
    payloads = {}
    for job in jobs:
        check = validate_pdf(Path(job['pdf']), job['metadata'])
        if check['status'] not in ('verified', 'probably_correct'):
            raise RuntimeError('Refusing unverified PDF attachment')
        payloads['/' + uuid.uuid4().hex + '.pdf'] = (job['key'], Path(job['pdf']).read_bytes())
    if not payloads:
        return dict(status='nothing_to_do', rows=[])
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in payloads:
                self.send_error(404)
                return
            data = payloads[self.path][1]
            self.send_response(200)
            self.send_header('Content-Type', 'application/pdf')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    streams = {key: 'http://127.0.0.1:' + str(server.server_port) + path
               for path, (key, data) in payloads.items()}
    try:
        return _execute(batch, action, jobs, wait_seconds, streams)
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def _execute(batch, action, jobs, wait_seconds=50, streams=None):
    entries = []
    for job in jobs:
        meta = job['metadata']
        entry = dict(key=job['key'], doi=meta['doi'], title=meta['title'])
        if action == 'attach':
            check = validate_pdf(Path(job['pdf']), meta)
            if check['status'] not in ('verified', 'probably_correct'):
                raise RuntimeError('Refusing unverified PDF attachment')
            if good(managed_pdfs(job['key'], meta)):
                continue
            entry['stream_url'] = streams[job['key']]
            entry['source_url'] = job.get('source_url') or meta['url']
        entries.append(entry)
    if not entries:
        return dict(status='nothing_to_do', rows=[])
    # Never overwrite an existing script window, possibly containing the user's work.
    windows = subprocess.run(['osascript', '-e',
        'tell application "System Events" to tell process "Zotero" to get name of every window'],
        capture_output=True, text=True, timeout=10)
    if windows.returncode or 'JavaScript' in windows.stdout:
        raise RuntimeError('Existing JavaScript window or unavailable UI: close/inspect it first')
    for job in jobs:
        prior = job.get('gui_report')
        if prior and Path(prior).exists():
            state = json.loads(Path(prior).read_text(encoding='utf-8'))
            if state['status'] == 'running':
                raise RuntimeError('Earlier Zotero batch is unresolved; inspect it before restarting')
    run_id = str(uuid.uuid4())
    report = batch.root / ('zotero-' + run_id + '.json')
    req = dict(run_id=run_id, collection=batch.collection, action=action, report=str(report), items=entries)
    code = 'const request=' + json.dumps(req, ensure_ascii=True) + ';\n' + Path(__file__).with_name('zotero_batch.js').read_text(encoding='utf-8')
    script_path = batch.root / ('zotero-' + run_id + '.js')
    script_path.write_text(code, encoding='utf-8')
    # Record the handle before sending any write. An uncertain UI launch is never retried blindly.
    for job in jobs:
        job['gui_report'] = str(report)
        job['gui_launch_status'] = 'launching'
        batch.save(job)
    launcher = '''on run argv
tell application "Zotero" to activate
delay 0.3
tell application "System Events" to tell process "Zotero"
click menu item "Run JavaScript" of menu "开发者" of menu item "开发者" of menu "工具" of menu bar 1
end tell
delay 1
tell application "System Events" to tell process "Zotero"
if name of front window does not contain "JavaScript" then error "Script window not active"
end tell
set the clipboard to (item 1 of argv)
tell application "System Events" to tell process "Zotero"
keystroke "a" using command down
keystroke "v" using command down
delay 0.3
keystroke "r" using command down
end tell
end run'''
    run = subprocess.run(['osascript', '-e', launcher, code], capture_output=True, text=True, timeout=20)
    if run.returncode:
        raise RuntimeError('UI launch uncertain: ' + run.stderr.strip())
    for _ in range(wait_seconds):
        if report.exists():
            try:
                result = json.loads(report.read_text(encoding='utf-8'))
                if result['run_id'] != run_id:
                    raise RuntimeError('Wrong job report')
                if result['status'] != 'running':
                    result = reconcile(batch, jobs, result)
                    result['window_cleanup'] = close_own_window(run_id)
                    return result
            except json.JSONDecodeError:
                pass  # Atomicity boundary: writer may still be flushing its checkpoint.
        time.sleep(1)
    return dict(status='pending', report=str(report), message='Do not relaunch; inspect this job and Zotero window')


def reconcile(batch, jobs, result):
    for job in jobs:
        rows = [r for r in result.get('rows', []) if r['key'] == job['key']]
        checks = managed_pdfs(job['key'], job['metadata'])
        job['gui_result'] = rows
        if good(checks):
            job.update(status='complete', attachments=checks)
        elif any(r['status'] == 'not_found' for r in rows):
            job['full_lookup_complete'] = True
        elif result.get('action') != 'probe':
            job['status'] = 'gui_result_needs_review'
        job['gui_launch_status'] = result['status']
        batch.save(job)
    batch.report()
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['probe', 'find', 'attach', 'reconcile'])
    p.add_argument('--collection', required=True)
    p.add_argument('--work-dir', required=True)
    p.add_argument('--prefs', required=True)
    p.add_argument('--doi', action='append', required=True)
    args = p.parse_args()
    b = Batch(args.work_dir, args.collection, args.prefs)
    jobs = []
    for doi in args.doi:
        row = b.db.execute('SELECT data FROM jobs WHERE doi=?', (doi_normalize(doi),)).fetchone()
        if not row:
            raise RuntimeError('No identified parent item in queue')
        jobs.append(json.loads(row[0]))
    if args.action == 'reconcile':
        outputs = []
        for job in jobs:
            r = json.loads(Path(job['gui_report']).read_text(encoding='utf-8'))
            outputs.append(reconcile(b, [job], r) if r['status'] != 'running' else r)
        print(json.dumps(outputs, ensure_ascii=False))
    else:
        print(json.dumps(execute(b, args.action, jobs), ensure_ascii=False))


if __name__ == '__main__':
    main()
