"""Regression tests for independently verified review findings."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from bot import Bot, Store, APIError
from ai import AIClient, ReviewError, ReviewWorker
from test_cases import API, sample, CID, CH
from policy import features, verdict

class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.config={'owner_id':1,'archive_channel':CH,'groups':{str(CID):{'mode':'review','blocked_domains':['example.com']}}}
        self.store=Store(Path(self.tmp.name)/'db',self.config)
        self.api=API();self.bot=Bot(self.api,self.store,{'id':99,'username':'test_bot'},1)
    def tearDown(self):self.store.db.close();self.tmp.cleanup()
    def test_successful_ban_notice_deleted_after_three_minutes_only(self):
        c=self.bot.cases.open(sample(),'test',dry=False)
        with patch('cases.time.time',return_value=1000):self.bot.cases.execute(c['id'],'admin:20')
        self.assertEqual(self.store.db.execute('SELECT due FROM case_notice_cleanup').fetchone()[0],1180)
        self.api.calls=[]
        self.bot.cases.cleanup_notices(1179);self.assertEqual(self.api.calls,[])
        self.bot.cases.cleanup_notices(1180)
        self.assertEqual(self.api.calls,[('deleteMessage',{'chat_id':CID,'message_id':c['report_id']})])
        self.bot.cases.cleanup_notices(2000);self.assertEqual(len(self.api.calls),1)
    def test_notice_cleanup_persists_restart_and_failed_ban_not_scheduled(self):
        from cases import Cases
        c=self.bot.cases.open(sample(),'test',dry=False)
        self.api.fail='banChatMember';self.bot.cases.execute(c['id'],'admin:20')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM case_notice_cleanup').fetchone()[0],0)
        self.api.fail=None
        c=self.bot.cases.open({**sample(),'message_id':8},'test',dry=False)
        self.bot.cases.execute(c['id'],'admin:20')
        reopened=Cases(self.bot,CH);self.api.calls=[];reopened.cleanup_notices(10**12)
        self.assertEqual(self.api.calls,[('deleteMessage',{'chat_id':CID,'message_id':c['report_id']})])
    def test_notice_cleanup_retry_is_bounded_and_does_not_ban(self):
        self.store.db.execute('INSERT INTO case_notice_cleanup(case_id,cid,mid,due) VALUES(1,?,100,0)',(CID,));self.store.db.commit()
        self.api.fail='deleteMessage'
        self.bot.cases.cleanup_notices(1000)
        self.assertEqual(self.store.db.execute('SELECT state FROM case_notice_cleanup').fetchone()[0],'failed')
        self.assertNotIn('banChatMember',[m for m,p in self.api.calls])

    def test_moderator_id_private_in_case_archive_and_audit(self):
        from cases import public_reason
        self.assertEqual(public_reason('处理来源：admin:123456789'),'处理来源：管理员确认')
        self.assertEqual(public_reason('普通广告理由'),'普通广告理由')
        c=self.bot.cases.open(sample(),'内部历史 admin:123456789',announce=False,dry=False)
        self.assertNotIn('123456789',self.bot.cases.text(c))
        self.bot.cases.execute(c['id'],'admin:123456789')
        for method,payload in self.api.calls:
            self.assertNotIn('123456789',payload.get('text',''))
        self.assertIn('admin:123456789',self.store.db.execute("SELECT reason FROM events WHERE action='case_banned'").fetchone()[0])
        self.bot.management.audit(CID,123456789,'unban',2,c['id'])
        text,_,_=self.bot.management.listing(CID,'audit',1)
        self.assertNotIn('123456789',text)
        self.assertEqual(self.store.db.execute("SELECT actor FROM management_audit WHERE action='unban'").fetchone()[0],123456789)

    def test_missing_archive_commands_never_mutate_domains(self):
        self.bot.cases=None
        for name in ('/adreview','/adcase'):
            self.bot.handle({'message':sample(uid=1,text=name+' example.com')})
            self.assertEqual(self.store.policy(CID)['blocked_domains'],['example.com'])
        self.assertTrue(any('未配置' in p.get('text','') for n,p in self.api.calls))
    def test_mode_permission_failure_has_reply_and_preserves_mode(self):
        p=self.store.policy(CID);p['mode']='observe';self.store.set('group:'+str(CID),p)
        for api_failure in (False,True):
            self.api.permissions=False;self.api.fail='getChatMember' if api_failure else None
            self.bot.handle({'message':sample(uid=1,text='/admode review CONFIRM')})
            self.assertEqual(self.store.policy(CID)['mode'],'observe')
        self.assertTrue(any('模式未变更' in p.get('text','') for n,p in self.api.calls))
    def test_clean_review_message_no_sync_role_query_and_one_classification(self):
        self.bot.ai=None
        with patch('bot.policy_verdict',wraps=verdict) as classify:
            self.bot.handle({'message':sample(text='今天吃饭了吗')})
        self.assertEqual(classify.call_count,1)
        self.assertNotIn('getChatMember',[n for n,p in self.api.calls])
    def test_queue_full_rule_hit_has_manual_case_without_countdown(self):
        self.bot.ai=type('AI',(),{'submit':lambda *a,**kw:False})()
        self.bot.handle({'message':sample()})
        row=self.store.db.execute('SELECT state,deadline FROM cases').fetchone()
        self.assertEqual(tuple(row),('pending',0))
        self.assertNotIn('banChatMember',[n for n,p in self.api.calls])
    def test_failed_uncertain_results_only_rule_hits_fallback_and_stale_skip(self):
        for mid,label in ((7,'error'),(8,'uncertain')):
            m={**sample(),'message_id':mid,'_ad_features':features(self.store.policy(CID))}
            self.bot.cases.track(m);self.bot.cases.ai_result(m,{'label':label})
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM cases WHERE deadline=0').fetchone()[0],2)
        m={**sample(),'message_id':9};self.bot.cases.track(m)
        self.bot.cases.track({**m,'text':'正常内容'})
        self.bot.cases.ai_result(m,{'label':'error'})
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM cases').fetchone()[0],2)
        m={**sample(text='普通聊天'),'message_id':10};self.bot.cases.track(m)
        self.bot.cases.ai_result(m,{'label':'error'})
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM cases').fetchone()[0],2)
    def test_admin_rule_hit_fallback_stays_protected(self):
        m=sample(uid=20);self.bot.cases.track(m)
        self.bot.cases.ai_result(m,{'label':'error'})
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM cases').fetchone()[0],0)
    def test_report_false_positive_examples(self):
        for text in ('到机场了联系我','这个服务器价格太高了','服务器配好了，@alice_dev 你看一下','Claude 账号 @someone 帮我看下','服务器 vxlan 配置好了'):
            with self.subTest(text=text):self.assertEqual(verdict({'text':text},{})['level'],'clean')
    def test_remote_ai_http_rejected_loopback_allowed(self):
        for url in ('http://provider.example/v1','http://127.0.0.1.evil.example/v1','https://user:password@provider.example/v1'):
            with self.assertRaises(ReviewError):AIClient({'base_url':url},'SECRET')
        for url in ('http://127.0.0.1:18317/v1','https://provider.example/v1'):
            AIClient({'base_url':url},'SECRET')
    def test_edit_creates_new_revision_and_old_votes_stay_cancelled(self):
        m=sample();self.bot.cases.track(m)
        old=self.bot.cases.open(m,'old',eligible=True)
        self.store.db.execute('INSERT INTO case_votes VALUES(?,?)',(old['id'],3));self.store.db.commit()
        edited={**m,'text':m['text']+' 加我购买'};self.bot.cases.track(edited)
        new=self.bot.cases.open(edited,'new',eligible=True)
        self.assertNotEqual(old['id'],new['id'])
        self.assertEqual(self.bot.cases.get(old['id'])['state'],'edited')
        self.assertEqual(new['revision'],1)
        self.assertEqual(new['mid'],old['mid'])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM case_votes WHERE case_id=?',(new['id'],)).fetchone()[0],0)
        self.assertEqual(self.bot.cases.open(edited,'duplicate')['id'],new['id'])
    def test_overflow_retained_manual_only(self):
        for mid in range(20):self.bot.cases.open({**sample(),'message_id':100+mid},'test',eligible=True)
        count=len([n for n,p in self.api.calls if n=='sendMessage'])
        c=self.bot.cases.open({**sample(),'message_id':200},'overflow',eligible=True)
        self.assertEqual((c['state'],c['deadline'],c['report_id']),('held',0,0))
        self.assertEqual(len([n for n,p in self.api.calls if n=='sendMessage']),count)
        self.assertEqual(self.store.db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
    def test_old_schema_migration_preserves_ids_and_related_rows(self):
        from cases import Cases
        self.store.db.executescript("DROP INDEX case_pending; ALTER TABLE cases RENAME TO cases_new; CREATE TABLE cases(id INTEGER PRIMARY KEY AUTOINCREMENT,cid INTEGER,mid INTEGER,uid INTEGER,created INTEGER,deadline INTEGER,state TEXT,reason TEXT,payload TEXT,report_id INTEGER DEFAULT 0,archive_id INTEGER DEFAULT 0,dry INTEGER DEFAULT 1,learn_text TEXT DEFAULT '',archive_text TEXT DEFAULT '',UNIQUE(cid,mid)); DROP TABLE cases_new;")
        self.store.db.execute("INSERT INTO cases(id,cid,mid,uid,state,payload) VALUES(42,?,?,2,'edited','{}')",(CID,7))
        self.store.db.execute('INSERT INTO case_votes VALUES(42,3)');self.store.db.commit()
        cases=Cases(self.bot,CH)
        self.assertEqual(cases.get(42)['revision'],0)
        self.assertEqual(self.store.db.execute('SELECT voter FROM case_votes WHERE case_id=42').fetchone()[0],3)
        self.assertTrue((Path(self.tmp.name)/'schema-before-revisions.db').exists())
        new=cases.open(sample(),'new',announce=False)
        self.assertGreater(new['id'],42);self.assertEqual(new['revision'],1)

    def test_worker_failure_and_expiry_emit_fallback(self):
        worker=ReviewWorker(type('Client',(),{})(),self.api,Path(self.tmp.name)/'ai-db',1);worker.close()
        worker.stop.clear()
        m=sample();worker.submit(m)
        def failing(*args):
            worker.stop.set();raise ReviewError('failed')
        worker.process=failing;worker.run()
        self.assertEqual(worker.results.get_nowait()[1]['label'],'error')
        worker.stop.clear();worker.submit(m)
        def expired(message):
            ReviewWorker.failure_result(worker,message);worker.stop.set()
        worker.failure_result=expired
        with patch('ai.time.monotonic',return_value=10**12):worker.run()
        self.assertEqual(worker.expired,1)
        self.assertEqual(worker.results.get_nowait()[1]['label'],'error')

if __name__=='__main__':unittest.main()
