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
