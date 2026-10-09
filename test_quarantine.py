import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from bot import Bot,Store,APIError
from test_cases import API,CID,CH,sample
from quarantine import PERMISSIONS

class QuarantineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.api=API();self.members={};original=self.api.call
        def call(method,**kw):
            if method=='getChatMember' and kw['user_id'] in self.members:
                self.api.calls.append((method,kw));return dict(self.members[kw['user_id']])
            if method=='getChat':return {'permissions':{'can_send_messages':True,'can_send_photos':True,'can_invite_users':False}}
            if method=='restrictChatMember':
                self.api.calls.append((method,kw))
                if self.api.fail==method:raise APIError(403)
                self.members[kw['user_id']]={'status':'restricted','until_date':kw['until_date'],**kw['permissions']}
                if kw['until_date']==0:self.members[kw['user_id']]={'status':'member'}
                return True
            return original(method,**kw)
        self.api.call=call
        self.config={'review_flow':'quarantine','owner_id':1,'archive_channel':CH,'groups':{str(CID):{'mode':'review','blocked_domains':[]}}}
        self.store=Store(Path(self.temp.name)/'db',self.config)
        self.bot=Bot(self.api,self.store,{'id':99,'username':'test_bot'},1);self.cases=self.bot.cases
    def tearDown(self):self.store.db.close();self.temp.cleanup()
    def create(self,eligible=True,message=None):return self.cases.open(message or sample(),'广告证据',eligible=eligible,dry=False)
    def methods(self):return [m for m,p in self.api.calls]
    def callback(self,c,action,uid=20):
        self.cases.callback({'id':'q','data':'adcase:'+action+':'+str(c['id']),'from':{'id':uid},
            'message':{'chat':{'id':CID},'message_id':self.cases.get(c['id'])['report_id']}})
    def test_immediate_containment_but_no_archive_learning_or_ban(self):
        c=self.create();q=self.cases.quarantine.get(c['id'])
        self.assertEqual(c['deadline'],0);self.assertEqual(q['phase'],'muted');self.assertEqual(q['deleted'],1)
        self.assertIn('restrictChatMember',self.methods());self.assertIn('deleteMessage',self.methods())
        self.assertNotIn('banChatMember',self.methods());self.assertNotIn('copyMessage',self.methods())
        self.assertEqual(self.store.db.execute('select count(*) from case_samples').fetchone()[0],0)
        self.assertIn('原文摘录：',self.cases.text(c))
    def test_weak_evidence_has_card_without_early_punishment(self):
        c=self.create(False)
        self.assertFalse(self.cases.quarantine.get(c['id']))
        self.assertNotIn('restrictChatMember',self.methods());self.assertNotIn('deleteMessage',self.methods())
    def test_owner_admin_and_whitelist_protected(self):
        self.assertIsNone(self.create(message=sample(uid=1)))
        self.assertIsNone(self.create(message=sample(uid=20)))
        self.assertNotIn('restrictChatMember',self.methods())
    def test_no_review_keeps_card_and_never_bans(self):
        c=self.create();q=self.cases.quarantine.get(c['id'])
        with patch('cases.time.time',return_value=q['until_ts']+1):self.cases.tick()
        self.assertEqual(self.cases.get(c['id'])['state'],'pending')
        self.assertEqual(self.cases.quarantine.get(c['id'])['phase'],'expired')
        self.assertNotIn('banChatMember',self.methods())
        self.assertNotIn(c['report_id'],[p['message_id'] for method,p in self.api.calls if method=='deleteMessage'])
    def test_admin_reject_restores_current_group_defaults(self):
        c=self.create();self.callback(c,'reject')
        self.assertEqual(self.cases.get(c['id'])['state'],'admin_rejected')
        self.assertEqual(self.cases.quarantine.get(c['id'])['phase'],'released')
        restore=[p for method,p in self.api.calls if method=='restrictChatMember'][-1]
        self.assertTrue(restore['permissions']['can_send_messages']);self.assertFalse(restore['permissions']['can_invite_users'])
    def test_three_distinct_nonself_votes_restore(self):
        c=self.create()
        for uid in (2,3,3,4):self.callback(c,'vote',uid)
        self.assertEqual(self.cases.get(c['id'])['state'],'pending')
        self.callback(c,'vote',5)
        self.assertEqual(self.cases.get(c['id'])['state'],'member_rejected')
        self.assertEqual(self.cases.quarantine.get(c['id'])['phase'],'released')
    def test_changed_external_restriction_is_not_overwritten(self):
        c=self.create();self.members[2]['until_date']+=100
        count=self.methods().count('restrictChatMember');self.callback(c,'reject')
        self.assertEqual(self.methods().count('restrictChatMember'),count)
        self.assertEqual(self.cases.get(c['id'])['state'],'pending')
        self.assertEqual(self.cases.quarantine.get(c['id'])['phase'],'release_conflict')
    def test_existing_restriction_not_touched(self):
        self.members[2]={'status':'restricted','until_date':0,'can_send_messages':False}
        c=self.create();self.callback(c,'reject')
        self.assertNotIn('restrictChatMember',self.methods())
        self.assertEqual(self.members[2]['until_date'],0)
    def test_confirm_archives_bans_removes_card_posts_temporary_notice(self):
        c=self.create();self.callback(c,'ban');q=self.cases.quarantine.get(c['id'])
        self.assertEqual(self.cases.get(c['id'])['state'],'banned')
        self.assertIn('copyMessage',self.methods());self.assertIn('banChatMember',self.methods())
        self.assertGreater(q['notice_id'],0)
        self.assertIn(c['report_id'],[p['message_id'] for method,p in self.api.calls if method=='deleteMessage'])
        row=self.store.db.execute('select due,state from quarantine_cleanup where mid=?',(q['notice_id'],)).fetchone()
        self.assertEqual(row['state'],'pending')
        with patch('cases.time.time',return_value=row['due']):self.cases.tick()
        self.assertIn(q['notice_id'],[p['message_id'] for method,p in self.api.calls if method=='deleteMessage'])
        archive=self.cases.get(c['id'])['archive_id']
        self.assertNotIn(archive,[p['message_id'] for method,p in self.api.calls if method=='deleteMessage' and p['chat_id']==CH])
    def test_duplicate_confirm_never_bans_twice(self):
        c=self.create();self.callback(c,'ban');self.callback(c,'ban')
        self.assertEqual(self.methods().count('banChatMember'),1)
    def test_channel_failure_preserves_pending_mute_and_does_not_ban(self):
        c=self.create();self.api.fail='copyMessage';self.callback(c,'ban')
        self.assertEqual(self.cases.get(c['id'])['state'],'held')
        self.assertEqual(self.cases.quarantine.get(c['id'])['phase'],'muted')
        self.assertNotIn('banChatMember',self.methods())
    def test_failed_mute_does_not_delete_or_claim_success(self):
        self.api.fail='restrictChatMember';c=self.create()
        self.assertNotIn('deleteMessage',self.methods())
        self.assertEqual(self.cases.quarantine.get(c['id'])['phase'],'mute_uncertain')
    def test_restart_preserves_mute_and_no_timeout_ban(self):
        c=self.create();self.store.db.close();self.store=Store(Path(self.temp.name)/'db',self.config)
        bot=Bot(self.api,self.store,{'id':99,'username':'test_bot'},1)
        self.assertEqual(bot.cases.quarantine.get(c['id'])['phase'],'muted')
        self.assertEqual(bot.cases.get(c['id'])['deadline'],0)
    def test_observation_has_no_containment(self):
        self.cases.open(sample(),'观察',eligible=True,dry=True)
        self.assertNotIn('deleteMessage',self.methods());self.assertNotIn('restrictChatMember',self.methods())
    def test_release_failure_keeps_case_actionable_for_admin_retry(self):
        c=self.create();self.api.fail='restrictChatMember';self.callback(c,'reject')
        self.assertEqual(self.cases.get(c['id'])['state'],'pending')
        self.assertEqual(self.cases.quarantine.get(c['id'])['phase'],'release_failed')
        self.api.fail=None;self.callback(c,'reject')
        self.assertEqual(self.cases.get(c['id'])['state'],'admin_rejected')
    def test_failed_card_send_retains_containment_and_notifies_owner(self):
        notices=[];self.api.enqueue_send=lambda **p:notices.append(p)
        self.api.fail='sendMessage';c=self.create()
        self.assertEqual(c['state'],'held');self.assertEqual(c['report_id'],0)
        self.assertEqual(self.cases.quarantine.get(c['id'])['phase'],'muted')
        self.assertEqual(notices[0]['chat_id'],1)
    def test_management_reject_works_even_without_group_card(self):
        c=self.create();self.store.db.execute('update cases set report_id=0 where id=?',(c['id'],));self.store.db.commit()
        message={'chat':{'id':CID},'message_id':55,'from':{'id':20}}
        self.bot.management.panel(message,CID,'history')
        p=self.store.db.execute('select token,mid from management_panels').fetchone()
        query={'id':'q','data':'adm:'+p['token']+':reject:'+str(c['id']),'from':{'id':20},'message':{'chat':{'id':CID},'message_id':p['mid']}}
        self.bot.management.callback(query)
        self.assertEqual(self.cases.get(c['id'])['state'],'admin_rejected')
    def test_member_cannot_confirm_from_management_panel(self):
        c=self.create();message={'chat':{'id':CID},'message_id':55,'from':{'id':20}}
        self.bot.management.panel(message,CID,'history');p=self.store.db.execute('select token,mid from management_panels').fetchone()
        self.bot.management.callback({'id':'q','data':'adm:'+p['token']+':confirm:'+str(c['id']),'from':{'id':3},'message':{'chat':{'id':CID},'message_id':p['mid']}})
        self.assertNotIn('banChatMember',self.methods())
    def test_existing_deadlines_cancelled_on_flow_upgrade(self):
        c=self.create(False);self.store.db.execute('update cases set deadline=1 where id=?',(c['id'],));self.store.db.commit()
        upgraded=Bot(self.api,self.store,{'id':99,'username':'test_bot'},1)
        self.assertEqual(upgraded.cases.get(c['id'])['deadline'],0)

if __name__=='__main__':unittest.main()
