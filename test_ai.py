import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from ai import AIClient, ReviewError, ReviewWorker, parse_review

class AIValidationTests(unittest.TestCase):
    def test_invalid_or_instruction_response_fails_closed(self):
        for text in ('delete this user', '{}', '{"label":"spam","reason":{},"observed_text":""}', '```\nnot json\n```'):
            with self.assertRaises(ReviewError):parse_review(text)
    def test_valid_json_and_bounded_fields(self):
        v=parse_review(json.dumps({'label':'uncertain','reason':'x'*1000,'observed_text':'y'*1000}))
        self.assertEqual(len(v['reason']),200);self.assertEqual(len(v['observed_text']),512)
    def test_provider_exception_cannot_leak_key(self):
        api=type('API',(),{})()
        with patch('urllib.request.urlopen',side_effect=ValueError('SECRET')):
            with self.assertRaises(ReviewError) as caught:AIClient({'base_url':'http://localhost/v1','model':'test'},'SECRET').review({'text':'hello'},api)
        self.assertNotIn('SECRET',str(caught.exception))

class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.api=type('API',(),{})();self.calls=[]
        self.api.call=lambda method,**kw:self.calls.append(method) or {'status':'member'}
        self.api.enqueue_send=lambda **kw:self.calls.append('sendMessage')
        self.client=type('Client',(),{})();self.client.review=lambda *args:{'label':'spam','reason':'广告','observed_text':'现货优惠出售联系店主'}
        self.worker=ReviewWorker(self.client,self.api,Path(self.temp.name)/'db',1)
        self.message={'chat':{'id':-123},'message_id':7,'from':{'id':2},'text':'待检查'}
    def tearDown(self):
        self.worker.close();self.temp.cleanup()
    def test_spam_review_never_deletes_bans_or_learns(self):
        self.worker.process(self.message,None)
        import sqlite3
        with sqlite3.connect(self.worker.path) as db:
            self.assertEqual(db.execute('SELECT label FROM ai_reviews').fetchone()[0],'spam')
        self.assertEqual(self.calls,['getChatMember'])
    def test_admin_and_owner_exempt(self):
        self.api.call=lambda *a,**k:{'status':'administrator'}
        self.client.review=lambda *a:self.fail('admin must not invoke AI')
        self.worker.process(self.message,None)
        self.worker.process({**self.message,'from':{'id':1}},None)
    def test_clean_ocr_not_saved(self):
        self.client.review=lambda *a:{'label':'normal','reason':'普通聊天','observed_text':'private content'}
        self.worker.process(self.message,None)
        import sqlite3
        with sqlite3.connect(self.worker.path) as db:self.assertEqual(db.execute('SELECT observed_text FROM ai_reviews').fetchone()[0],'')
    def test_duplicate_automatic_delivery_does_not_call_model_twice(self):
        calls=[]
        self.client.review=lambda *a:calls.append(1) or {'label':'spam','reason':'广告','observed_text':''}
        self.worker.process(self.message,None);self.worker.process(self.message,None)
        self.assertEqual(len(calls),1)
        self.assertEqual(self.calls.count('getChatMember'),1)
    def test_failed_review_does_not_poison_duplicate_cache(self):
        self.client.review=lambda *a:(_ for _ in ()).throw(ReviewError('failed'))
        with self.assertRaises(ReviewError):self.worker.process(self.message,None)
        self.assertNotIn((-123,7),self.worker.seen)
    def test_manual_jobs_have_priority(self):
        self.worker.close()
        self.worker.submit(self.message)
        self.worker.submit(self.message,reply=self.message)
        self.assertIsNotNone(self.worker.jobs.get_nowait()[-1])
    def test_suspicious_and_photo_jobs_precede_normal_text(self):
        self.worker.close()
        self.worker.submit(self.message)
        self.worker.submit({**self.message,'photo':[{'file_id':'sample'}]})
        self.worker.submit(self.message,urgent=True)
        self.worker.submit(self.message,reply=self.message)
        self.assertEqual([self.worker.jobs.get_nowait()[0] for _ in range(4)],[0,1,2,3])
    def test_health_alert_only_after_three_failures_and_once_until_recovery(self):
        notices=[]
        self.api.enqueue_send=lambda **kw:notices.append(kw)
        self.worker.health_failure();self.worker.health_failure()
        self.assertEqual(notices,[])
        self.worker.health_failure();self.worker.health_failure()
        self.assertEqual(len(notices),1)
        self.assertEqual(notices[0]['chat_id'],1)
        self.worker.health_success();self.worker.health_success()
        self.assertEqual(len(notices),2)
        self.assertEqual(self.worker.consecutive_failures,0)
        for _ in range(3):self.worker.health_failure()
        self.assertEqual(len(notices),3)
    def test_cache_hit_does_not_claim_provider_recovery(self):
        self.worker.process(self.message,None)
        for _ in range(3):self.worker.health_failure()
        self.worker.process({**self.message,'message_id':8},None)
        self.assertTrue(self.worker.alerted)
        self.assertEqual(self.worker.consecutive_failures,3)
    def test_queue_overflow_counted_and_returns_false(self):
        self.worker.close()
        for _ in range(32):self.assertTrue(self.worker.submit(self.message))
        self.assertFalse(self.worker.submit(self.message))
        self.assertEqual(self.worker.queue_full,1)
    def test_status_distinguishes_wait_from_model_time(self):
        self.worker.queue_waits=[2,4,6]
        self.worker.review_times=[1,3,5]
        self.assertIn('排队中位耗时：4秒',self.worker.status())
        self.assertIn('模型中位耗时：3秒',self.worker.status())
    def test_status_includes_failure_and_queue_not_just_enabled(self):
        self.worker.failures=3
        self.assertIn('失败：3',self.worker.status())
        self.assertIn('/32',self.worker.status())
    def test_candidate_retention_100(self):
        for mid in range(110):
            self.worker.process({**self.message,'message_id':mid},None)
            self.worker.results.get_nowait()
        import sqlite3
        with sqlite3.connect(self.worker.path) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM ai_reviews').fetchone()[0],100)

if __name__=='__main__':unittest.main()
