#!/usr/bin/env python3
"""Save the PDF already delivered to the visible AbleSci download link.

No publisher/paywall/CDN request, cookies, decryption or browser setting changes.
Reads only the page's user-facing blob link, then validates before attachment.
"""
import argparse
import base64
import hashlib
import json
import time
import uuid
from pathlib import Path
from ablesci import AbleSci, chrome, read_chrome, transfer_state


def save_received(directory, doi, downloads_dir):
    adapter = AbleSci(directory)
    found = adapter.collect_local(doi, downloads_dir)
    if found:
        return found
    job = adapter.job(doi)
    if job.get('download_review'):
        return {'status': 'download_needs_review', 'review': job['download_review']}
    state = transfer_state()
    if not state['complete'] or state['url'] != job.get('download_attempt', {}).get('url'):
        raise RuntimeError('Only the exact completed transfer may be exported')
    token = 'scansci_export_' + uuid.uuid4().hex
    key = json.dumps(token)
    expected = json.dumps(state['url'])
    try:
        chrome('''(()=>{if(location.href!==''' + expected + ''')throw Error('Wrong page');
      const a=Array.from(document.querySelectorAll('a')).filter(e=>e.getClientRects().length&&e.innerText.trim()==='手动保存文件');
      if(a.length!==1||!a[0].href.startsWith('blob:https://www.ablesci.com/'))throw Error('No unique delivered blob');
      const task={status:'reading'};globalThis[''' + key + ''']=task;
      fetch(a[0].href).then(r=>r.blob()).then(blob=>{if(blob.size>50*1024*1024)throw Error('PDF too large');
        const reader=new FileReader();reader.onload=()=>{task.data=reader.result;task.status='ready'};
        reader.onerror=()=>{task.status='failed'};reader.readAsDataURL(blob);
      }).catch(()=>{task.status='failed'});return JSON.stringify({started:true});})()''')
        for _ in range(30):
            result = read_chrome('JSON.stringify({status:globalThis[' + key + ']?.status,length:globalThis[' + key + ']?.data?.length})')
            if result.get('status') == 'ready':
                break
            if result.get('status') != 'reading':
                raise RuntimeError('Delivered blob unavailable')
            time.sleep(1)
        else:
            raise RuntimeError('Blob export timed out; no new download started')
        if not isinstance(result.get('length'), int) or not 0 < result['length'] <= 70 * 1024 * 1024:
            raise RuntimeError('Invalid blob export length')
        parts = []
        for offset in range(0, result['length'], 131072):
            part = read_chrome('JSON.stringify(globalThis[' + key + '].data.slice(' + str(offset) + ',' + str(offset+131072) + '))')
            parts.append(part)
        data_url = ''.join(parts)
        header, encoded = data_url.split(',', 1)
        if ';base64' not in header:
            raise RuntimeError('Unexpected blob encoding')
        content = base64.b64decode(encoded, validate=True)
        if not content.startswith(b'%PDF-'):
            raise RuntimeError('Delivered bytes are not a PDF; not saved or accepted')
        digest = hashlib.sha256(content).hexdigest()
        target = Path(downloads_dir) / ('scansci-' + digest[:20] + '.pdf')
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise RuntimeError('Existing filename hash conflict')
        else:
            with target.open('xb') as stream:
                stream.write(content)
        found = adapter.collect_local(doi, downloads_dir)
        return found or {'status':'saved_needs_review','pdf':str(target),'review':adapter.job(doi).get('download_review')}
    finally:
        try:
            chrome('delete globalThis[' + key + '];JSON.stringify({cleaned:true})')
        except Exception:
            # This random, owned cache vanishes on navigation. Cleanup failure
            # must not misreport a validated managed/local PDF as a failed transfer.
            pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--work-dir', required=True)
    parser.add_argument('--doi', required=True)
    parser.add_argument('--downloads-dir', required=True)
    args = parser.parse_args()
    print(json.dumps(save_received(args.work_dir, args.doi, args.downloads_dir), ensure_ascii=False))
