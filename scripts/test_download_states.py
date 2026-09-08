"""Regression tests for observed transfer, budget and orchestration failures."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import Batch
from ablesci import AbleSci, navigate, read_chrome, chrome
from batch import finish_ablesci, requested_complete


class DownloadStateTests(unittest.TestCase):
    def setUp(self):
        parent=Path('_work/pdf_pipeline_tests');parent.mkdir(parents=True,exist_ok=True)
        self.root=Path(tempfile.mkdtemp(dir=parent))
        self.b=Batch(self.root,'TEST',self.root/'prefs.js')
        self.a=AbleSci(self.root)
        self.doi='10.1234/first'
        self.url='https://www.ablesci.com/assist/download?id=observed'
        self.job={'doi':self.doi,'status':'needs_ablesci','metadata':{'title':'Example study'},
                  'download_attempt':{'url':self.url,'started_ns':1}}
        self.b.save(self.job)
        self.a.db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?)',
                          (self.doi,'submitted',10,'Example study','https://www.ablesci.com/assist/detail?id=observed',0))
        self.a.db.commit()
        self.surface=patch('batch.transfer_state',return_value={'url':self.url,'active':False})
        self.surface.start();self.addCleanup(self.surface.stop)

    def test_navigation_refuses_active_transfer(self):
        with patch('ablesci.transfer_state',return_value={'url':self.url,'active':True}), \
             patch('ablesci.chrome',side_effect=AssertionError('must not navigate')):
            with self.assertRaisesRegex(RuntimeError,'still active'):
                navigate('https://www.ablesci.com/assist/create')

    def test_read_only_empty_response_is_retried(self):
        with patch('ablesci.chrome',side_effect=[json.JSONDecodeError('empty','',0),{'ready':True}]) as read, \
             patch('ablesci.time.sleep'):
            self.assertEqual(read_chrome('JSON.stringify({ready:true})'),{'ready':True})
        self.assertEqual(read.call_count,2)

    def test_write_empty_response_is_never_replayed(self):
        import subprocess
        response=subprocess.CompletedProcess([],0,'','')
        with patch('ablesci.subprocess.run',return_value=response) as write:
            with self.assertRaises(json.JSONDecodeError):chrome('button.click();JSON.stringify({clicked:true})')
        self.assertEqual(write.call_count,1)

    def test_fast_not_ready_is_not_an_attempt(self):
        state={'url':self.url,'downloading':True,'text':'高速下载扣 2 积分',
               'buttons':[{'id':'download-highspeed-direct','disabled':True}]}
        with patch('ablesci.transfer_state',return_value=state), \
             patch('ablesci.chrome',side_effect=AssertionError('must wait for button')):
            self.assertIsNone(self.a.enable_fast(self.doi,50,300))
        self.assertEqual(self.a.db.execute('SELECT COUNT(*) FROM speed_budget').fetchone()[0],0)

    def test_same_url_does_not_restart_transfer(self):
        with patch('ablesci.transfer_state',return_value={'url':self.url,'active':True}), \
             patch('ablesci.chrome',side_effect=AssertionError('must not reload')):
            navigate(self.url)

    def test_slow_progress_survives_45_second_boundary(self):
        clock=[0.0]
        def sleep(n):clock[0]+=n
        def state():
            return {'url':self.url,'active':True,'downloading':True,'failed':False,
                    'complete':False,'percent':clock[0],'text':'正在下载','buttons':[]}
        def local(*args):return {'status':'download_verified'} if clock[0]>=63 else None
        with patch.object(self.a,'collect_local',side_effect=local), \
             patch('ablesci.transfer_state',side_effect=state), \
             patch('ablesci.time.monotonic',side_effect=lambda:clock[0]), \
             patch('ablesci.time.sleep',side_effect=sleep), \
             patch('ablesci.navigate',side_effect=AssertionError('must preserve transfer')):
            result=self.a.download(self.doi,self.root,wait_seconds=90)
        self.assertEqual(result['status'],'download_verified')
        self.assertGreaterEqual(clock[0],63)

    def test_timeout_is_pending_not_permission_to_navigate(self):
        clock=[0.0]
        def sleep(n):clock[0]+=n
        state={'url':self.url,'active':True,'downloading':True,'failed':False,
               'complete':False,'percent':1,'text':'正在下载','buttons':[]}
        with patch.object(self.a,'collect_local',return_value=None), \
             patch('ablesci.transfer_state',return_value=state), \
             patch('ablesci.time.monotonic',side_effect=lambda:clock[0]), \
             patch('ablesci.time.sleep',side_effect=sleep), \
             patch('ablesci.navigate',side_effect=AssertionError('must not navigate')):
            self.assertEqual(self.a.download(self.doi,self.root,wait_seconds=9)['status'],'download_pending')

    def test_pending_transfer_prevents_next_request(self):
        self.b.save({'doi':'10.1234/second','status':'needs_ablesci','metadata':{}})
        with patch('batch.AbleSci',return_value=self.a), \
             patch.object(self.a,'download',return_value={'status':'download_pending'}), \
             patch.object(self.a,'prepare',side_effect=AssertionError('must not post next paper')):
            result=finish_ablesci(self.b,[self.doi,'10.1234/second'],self.root,50,300)
        self.assertEqual(len(result),2)

    def test_waiting_request_refreshes_to_discover_new_upload(self):
        self.job.pop('download_attempt')
        self.b.save(self.job)
        visible={'fresh':False}
        def navigate_request(url, **kwargs):
            self.assertEqual(url,'https://www.ablesci.com/assist/detail?id=observed')
            visible['fresh']=kwargs.get('refresh',False)
        def reconcile_request(doi):
            return {'status':'file_available' if visible['fresh'] else 'waiting'}
        with patch('batch.AbleSci',return_value=self.a), \
             patch('batch.navigate',side_effect=navigate_request), \
             patch.object(self.a,'reconcile',side_effect=reconcile_request), \
             patch.object(self.a,'download',return_value={'status':'download_pending'}) as download:
            finish_ablesci(self.b,[self.doi],self.root,50,300,wait_seconds=0)
        download.assert_called_once()

    def test_fast_budget_reservation_blocks_overspend_before_click(self):
        state={'url':self.url,'downloading':True,'text':'高速下载扣 2 积分',
               'buttons':[{'id':'download-highspeed-direct','disabled':False}]}
        with patch('ablesci.transfer_state',return_value=state), \
             patch('ablesci.chrome',side_effect=AssertionError('no spending click allowed')):
            self.assertFalse(self.a.enable_fast(self.doi,50,11))
        self.assertEqual(self.a.db.execute('SELECT COUNT(*) FROM speed_budget').fetchone()[0],0)

    def test_acceptance_requires_authority_before_browser(self):
        with patch('ablesci.navigate',side_effect=AssertionError('no browser')):
            with self.assertRaisesRegex(RuntimeError,'authorization'):
                self.a.accept_verified(self.doi)

    def test_acceptance_requires_pdf_before_browser(self):
        with patch('ablesci.navigate',side_effect=AssertionError('no browser')):
            with self.assertRaisesRegex(RuntimeError,'must be verified'):
                self.a.accept_verified(self.doi,approved=True)

    def test_uncertain_fast_reservation_never_confirms_twice(self):
        self.a.db.execute('INSERT INTO speed_budget VALUES(?,?,?)',(self.doi,2,'reserved_uncertain'))
        self.a.db.commit()
        state={'url':self.url,'downloading':True,'text':'高速下载扣 2 积分',
               'buttons':[{'id':'download-highspeed-direct','disabled':False}]}
        with patch('ablesci.transfer_state',return_value=state), \
             patch('ablesci.chrome',side_effect=AssertionError('cannot repeat payment')):
            with self.assertRaisesRegex(RuntimeError,'payment uncertain'):
                self.a.enable_fast(self.doi,50,300)

    def test_completed_transfer_with_review_does_not_retry(self):
        self.job['download_review']={'validation':{'reason':'fewer_pages_than_published_range'}}
        self.b.save(self.job)
        state={'url':self.url,'active':False,'complete':True,'percent':None,'text':'下载已完成'}
        with patch.object(self.a,'collect_local',return_value=None), \
             patch('ablesci.transfer_state',return_value=state), \
             patch('ablesci.chrome',side_effect=AssertionError('must not redownload')):
            self.assertEqual(self.a.download(self.doi,self.root)['status'],'download_needs_review')

    def test_acceptance_required_in_completion(self):
        jobs=[{'doi':self.doi,'status':'complete','source':'ablesci'}]
        self.assertTrue(requested_complete(jobs,[self.doi]))
        self.assertFalse(requested_complete(jobs,[self.doi],accept=True))
        jobs[0]['ablesci_accepted']=True
        self.assertTrue(requested_complete(jobs,[self.doi],accept=True))
        self.assertFalse(requested_complete(jobs,[self.doi,'10.1234/missing']))

    def test_completed_transfer_exports_delivered_blob_without_click_or_fee(self):
        state={'url':self.url,'active':False,'complete':True,'percent':None,'text':'下载已完成'}
        with patch.object(self.a,'collect_local',return_value=None), \
             patch('ablesci.transfer_state',return_value=state), \
             patch('ablesci.chrome',side_effect=AssertionError('no save/payment click')), \
             patch('save_received_blob.save_received',return_value={'status':'download_verified'}) as export:
            self.assertEqual(self.a.download(self.doi,self.root)['status'],'download_verified')
        export.assert_called_once_with(self.a.root,self.doi,self.root)
        self.assertEqual(self.a.db.execute('SELECT COUNT(*) FROM speed_budget').fetchone()[0],0)

    def test_live_transfer_is_prioritized_over_waiting_request(self):
        other='10.1234/waiting'
        self.b.save({'doi':other,'status':'needs_ablesci','metadata':{}})
        self.a.db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?)',
            (other,'submitted',10,'Other','https://www.ablesci.com/assist/detail?id=other',0))
        self.a.db.commit()
        with patch('batch.AbleSci',return_value=self.a), \
             patch.object(self.a,'download',return_value={'status':'download_pending'}) as download, \
             patch('batch.navigate',side_effect=AssertionError('must resume active first')):
            finish_ablesci(self.b,[other,self.doi],self.root,50,300)
        self.assertEqual(download.call_args.args[0],self.doi)


if __name__=='__main__':unittest.main()
