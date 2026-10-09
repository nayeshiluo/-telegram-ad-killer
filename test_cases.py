import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from bot import Bot,Store,APIError

CID=-1009000000001
CH=-1009000000002
def sample(uid=2,text='独家虚拟卡账号优惠批发限量出售联系客服立即购买'):
    return {'chat':{'id':CID,'type':'supergroup','title':'测试群'},'from':{'id':uid,'username':'demo_user'},'message_id':7,'text':text}

class API:
    def __init__(self):self.calls=[];self.roles={1:'creator',99:'administrator',20:'administrator'};self.fail=None;self.permissions=True;self.mid=100
    def call(self,name,**kw):
        self.calls.append((name,kw))
        if name==self.fail:raise APIError(403)
        if name=='getChatMember':return {'status':self.roles.get(kw['user_id'],'member'),'can_delete_messages':self.permissions,'can_restrict_members':self.permissions,'can_post_messages':self.permissions}
        if name in {'sendMessage','copyMessage'}:self.mid+=1;return {'message_id':self.mid}
        return True

class CaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.config={'owner_id':1,'archive_channel':CH,'groups':{str(CID):{'mode':'observe','blocked_domains':[]}}}
        self.store=Store(Path(self.temp.name)/'db',self.config)
        self.api=API();self.bot=Bot(self.api,self.store,{'id':99,'username':'test_bot'},1);self.cases=self.bot.cases
    def tearDown(self):self.store.db.close();self.temp.cleanup()
    def mode(self,value):
        p=self.store.policy(CID);p['mode']=value;self.store.set('group:'+str(CID),p)
    def methods(self):return [m for m,p in self.api.calls]
    def callback(self,c,action,uid=20,channel=False):
        self.cases.callback({'id':'q','data':'adcase:'+action+':'+str(c['id']),'from':{'id':uid},
            'message':{'chat':{'id':CH if channel else CID},'message_id':self.cases.get(c['id'])['archive_id'] if channel else c['report_id']}})
    def create(self,dry=True):return self.cases.open(sample(),'测试',eligible=True,dry=dry)
    def test_observe_button_simulates_no_actions(self):
        c=self.create();self.callback(c,'ban')
        self.assertEqual(self.cases.get(c['id'])['state'],'simulated_ban')
        self.assertNotIn('deleteMessage',self.methods());self.assertNotIn('banChatMember',self.methods())
    def test_three_unique_votes_and_self_rejection(self):
        c=self.create(False)
        for uid in (2,3,3,4):self.callback(c,'vote',uid)
        self.assertEqual(self.cases.get(c['id'])['state'],'pending')
        self.callback(c,'vote',5);self.assertEqual(self.cases.get(c['id'])['state'],'member_rejected')
    def test_member_cannot_confirm_ban(self):
        c=self.create(False);self.callback(c,'ban',3)
        self.assertEqual(self.cases.get(c['id'])['state'],'pending');self.assertNotIn('banChatMember',self.methods())
    def test_admin_reject_cancels_timer(self):
        c=self.create(False);self.callback(c,'reject')
        with patch('cases.time.time',return_value=c['deadline']+1):self.cases.tick()
        self.assertNotIn('banChatMember',self.methods())
    def test_archive_before_delete_before_ban_and_admin_learns(self):
        c=self.create(False);self.callback(c,'ban')
        self.assertEqual(self.cases.get(c['id'])['state'],'banned')
        self.assertLess(self.methods().index('copyMessage'),self.methods().index('deleteMessage'))
        self.assertLess(self.methods().index('deleteMessage'),self.methods().index('banChatMember'))
        self.assertEqual(self.store.db.execute('SELECT active FROM case_samples').fetchone()[0],1)
    def test_target_promoted_admin_after_case_open_protected(self):
        c=self.create(False);self.api.roles[2]='administrator';self.callback(c,'ban')
        self.assertEqual(self.cases.get(c['id'])['state'],'protected');self.assertNotIn('deleteMessage',self.methods())
    def test_target_promoted_while_archiving_not_deleted(self):
        c=self.create(False);original=self.api.call
        def call(method,**kw):
            result=original(method,**kw)
            if method=='copyMessage':self.api.roles[2]='administrator'
            return result
        self.api.call=call;self.callback(c,'ban')
        self.assertNotIn('deleteMessage',self.methods());self.assertEqual(self.cases.get(c['id'])['state'],'protected')
    def test_target_promoted_after_deletion_not_banned_or_learned(self):
        c=self.create(False);original=self.api.call
        def call(method,**kw):
            result=original(method,**kw)
            if method=='deleteMessage':self.api.roles[2]='administrator'
            return result
        self.api.call=call;self.callback(c,'ban')
        self.assertNotIn('banChatMember',self.methods());self.assertEqual(self.cases.get(c['id'])['state'],'deleted_protected')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM active_bans').fetchone()[0],0)
    def test_archive_failure_aborts_delete_and_ban(self):
        c=self.create(False);self.api.fail='copyMessage';self.callback(c,'ban')
        self.assertNotIn('deleteMessage',self.methods());self.assertNotIn('banChatMember',self.methods())
        self.assertEqual(self.cases.get(c['id'])['state'],'archiving_failed')
    def test_missing_original_archives_snapshot_and_can_ban(self):
        c=self.create(False);original=self.api.call
        def call(method,**params):
            if method in {'copyMessage','deleteMessage'}:
                self.api.calls.append((method,params));raise APIError(400,kind='message_missing')
            return original(method,**params)
        self.api.call=call;self.callback(c,'ban')
        row=self.cases.get(c['id'])
        self.assertEqual(row['state'],'banned')
        self.assertGreater(row['archive_id'],0)
        self.assertIn('接收时的快照',row['archive_text'])
        self.assertIn('banChatMember',self.methods())
        self.assertTrue(any(p.get('text')==sample()['text'] for method,p in self.api.calls if method=='sendMessage'))
    def test_missing_original_photo_reuses_saved_file_id(self):
        message=sample();message['photo']=[{'file_id':'stored-photo','width':800,'height':600}]
        c=self.cases.open(message,'测试',eligible=True,dry=False);original=self.api.call
        def call(method,**params):
            if method=='copyMessage':raise APIError(400,kind='message_missing')
            if method=='sendPhoto':
                self.api.calls.append((method,params));return {'message_id':777}
            return original(method,**params)
        self.api.call=call;self.cases.archive(c)
        self.assertTrue(any(p.get('photo')=='stored-photo' for method,p in self.api.calls if method=='sendPhoto'))
        self.assertIn('证据消息：777',self.cases.get(c['id'])['archive_text'])
    def test_snapshot_channel_failure_still_aborts_ban(self):
        c=self.create(False);original=self.api.call
        def call(method,**params):
            if method=='copyMessage':raise APIError(400,kind='message_missing')
            if method=='sendMessage' and params.get('chat_id')==CH:raise APIError(403)
            return original(method,**params)
        self.api.call=call;self.callback(c,'ban')
        self.assertEqual(self.cases.get(c['id'])['state'],'archiving_failed')
        self.assertNotIn('banChatMember',self.methods())
    def test_deleted_pending_notice_reposted_without_resetting_deadline(self):
        c=self.create(False);original=self.api.call
        def call(method,**params):
            if method=='editMessageText':raise APIError(400,kind='message_missing')
            return original(method,**params)
        self.api.call=call;self.cases.refresh(c['id'])
        current=self.cases.get(c['id'])
        self.assertNotEqual(current['report_id'],c['report_id'])
        self.assertEqual(current['deadline'],c['deadline'])
        self.assertEqual(current['state'],'pending')
    def test_deleted_completed_notice_is_not_reposted(self):
        c=self.create(False);self.cases.set_state(c['id'],'banned');original=self.api.call
        def call(method,**params):
            if method=='editMessageText':raise APIError(400,kind='message_missing')
            return original(method,**params)
        self.api.call=call;count=self.methods().count('sendMessage');self.cases.refresh(c['id'])
        self.assertEqual(count,self.methods().count('sendMessage'))
    def test_delete_failure_never_bans(self):
        c=self.create(False);self.api.fail='deleteMessage';self.callback(c,'ban')
        self.assertNotIn('banChatMember',self.methods())
        self.assertEqual(self.cases.get(c['id'])['state'],'delete_pending_failed')
    def test_ban_failure_no_active_id_or_learning(self):
        c=self.create(False);self.api.fail='banChatMember';self.callback(c,'ban')
        self.assertEqual(self.cases.get(c['id'])['state'],'ban_pending_failed')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM active_bans').fetchone()[0],0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM case_samples').fetchone()[0],0)
    def test_repeated_callback_does_not_ban_twice(self):
        c=self.create(False);self.callback(c,'ban');self.callback(c,'ban')
        self.assertEqual(self.methods().count('banChatMember'),1)
    def test_timeout_only_in_review_and_does_not_learn(self):
        self.mode('review');c=self.create(False)
        with patch('cases.time.time',return_value=c['deadline']+1):self.cases.tick()
        self.assertEqual(self.cases.get(c['id'])['state'],'banned')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM case_samples').fetchone()[0],0)
    def test_observe_timer_remains_dry(self):
        c=self.create()
        with patch('cases.time.time',return_value=c['deadline']+1):self.cases.tick()
        self.assertEqual(self.cases.get(c['id'])['state'],'simulated_ban');self.assertNotIn('banChatMember',self.methods())
    def test_stale_timer_after_outage_held(self):
        self.mode('review');c=self.create(False)
        with patch('cases.time.time',return_value=c['deadline']+121):self.cases.tick()
        self.assertEqual(self.cases.get(c['id'])['state'],'held');self.assertNotIn('banChatMember',self.methods())
    def test_message_edit_cancels_existing_case(self):
        m=sample();self.cases.track(m);c=self.create(False);self.cases.track({**m,'text':'改成正常聊天'})
        self.assertEqual(self.cases.get(c['id'])['state'],'edited')
    def test_invalid_forwarded_button_origin_rejected(self):
        c=self.create(False)
        self.cases.callback({'id':'q','data':'adcase:ban:'+str(c['id']),'from':{'id':20},'message':{'chat':{'id':-999},'message_id':c['report_id']}})
        self.assertNotIn('banChatMember',self.methods())
    def test_channel_admin_can_wrong_unban_and_deactivate_learning(self):
        c=self.create(False);self.callback(c,'ban');self.callback(c,'wrong',channel=True)
        self.assertEqual(self.cases.get(c['id'])['state'],'wrong_unbanned')
        self.assertEqual(self.store.db.execute('SELECT active FROM active_bans').fetchone()[0],0)
        self.assertEqual(self.store.db.execute('SELECT active FROM case_samples').fetchone()[0],0)
        call=next(p for m,p in self.api.calls if m=='unbanChatMember');self.assertTrue(call['only_if_banned'])
    def test_ordinary_channel_user_cannot_unban(self):
        c=self.create(False);self.callback(c,'ban');self.callback(c,'unban',uid=3,channel=True)
        self.assertNotIn('unbanChatMember',self.methods())
    def test_active_id_only_in_same_authorized_group(self):
        self.mode('review');c=self.create(False);self.callback(c,'ban')
        m={**sample(),'message_id':8,'text':'再次发言'};self.cases.track(m);self.cases.consider(m)
        self.assertEqual(self.methods().count('banChatMember'),2)
        m['chat']={'id':-888};self.assertFalse(self.cases.consider(m))
    def test_exact_learned_sample_other_member_banned_but_short_not(self):
        self.mode('review');c=self.create(False);self.callback(c,'ban')
        m={**sample(uid=3),'message_id':9};self.cases.consider(m)
        self.assertEqual(self.methods().count('banChatMember'),2)
        self.assertFalse(self.cases.consider({**sample(uid=4,text='优惠'),'message_id':10}))
    def test_stale_ai_result_after_edit_does_not_open_case(self):
        self.mode('review');m=sample();self.cases.track({**m,'text':'正常聊天'})
        self.cases.ai_result(m,{'label':'spam','reason':'广告','observed_text':''})
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM cases').fetchone()[0],0)
    def test_ai_only_weak_evidence_no_auto_deadline(self):
        self.mode('review');m=sample(text='神秘暗号去看看');self.cases.track(m)
        self.cases.ai_result(m,{'label':'spam','reason':'可能引流','observed_text':''})
        self.assertEqual(self.store.db.execute('SELECT deadline FROM cases').fetchone()[0],0)
    def test_admin_manual_command_routes_case_not_old_owner_only(self):
        m=sample(uid=20,text='/adkill CONFIRM');m['reply_to_message']=sample()
        self.bot.handle({'message':m});self.assertIn('banChatMember',self.methods())
    def test_pending_cases_survive_reopen(self):
        c=self.create(False)
        from cases import Cases
        other=Cases(self.bot,CH);self.assertEqual(other.get(c['id'])['state'],'pending')
    def test_permission_lookup_failure_no_destructive_actions(self):
        c=self.create(False);self.api.fail='getChatMember';self.cases.execute(c['id'],'timeout')
        self.assertNotIn('deleteMessage',self.methods());self.assertNotIn('banChatMember',self.methods())
    def test_old_archive_cannot_unban_newer_case(self):
        self.mode('review');old=self.create(False);self.callback(old,'ban')
        new=self.cases.open({**sample(),'message_id':9},'新广告',dry=False,announce=False);self.cases.execute(new['id'],'id_repeat')
        self.callback(old,'unban',channel=True)
        self.assertNotIn('unbanChatMember',self.methods());self.assertEqual(self.cases.get(old['id'])['state'],'superseded')
    def test_after_unban_wrong_button_can_still_revoke_learning(self):
        c=self.create(False);self.callback(c,'ban');self.callback(c,'unban',channel=True);self.callback(c,'wrong',channel=True)
        self.assertEqual(self.store.db.execute('SELECT active FROM case_samples').fetchone()[0],0)
    def test_delete_legacy_mode_archives_but_does_not_ban(self):
        policy=self.store.policy(CID);policy.update(mode='delete',blocked_domains=['example.com']);self.store.set('group:'+str(CID),policy)
        self.bot.handle({'message':sample(text='https://example.com')})
        self.assertIn('copyMessage',self.methods());self.assertIn('deleteMessage',self.methods());self.assertNotIn('banChatMember',self.methods())

    def test_dry_self_admin_can_test_rejection(self):
        c=self.cases.open(sample(uid=1),'观察自测',eligible=True,dry=True)
        self.callback(c,'reject',1)
        self.assertEqual(self.cases.get(c['id'])['state'],'admin_rejected')
        self.assertNotIn('deleteMessage',self.methods())
    def test_expired_notice_does_not_abort_valid_admin_reject(self):
        c=self.create(False);original=self.api.call
        def call(method,**kw):
            if method=='answerCallbackQuery':raise APIError(400,kind='expired_callback')
            return original(method,**kw)
        self.api.call=call;self.callback(c,'reject')
        self.assertEqual(self.cases.get(c['id'])['state'],'admin_rejected')
        self.assertNotIn('banChatMember',self.methods())
    def test_other_callback_failure_does_not_proceed(self):
        c=self.create(False);self.api.fail='answerCallbackQuery'
        with self.assertRaises(APIError):self.callback(c,'ban')
        self.assertNotIn('banChatMember',self.methods())

    def test_dry_finished_archived_without_real_unban_buttons(self):
        c=self.create();self.callback(c,'ban')
        archived=self.cases.get(c['id'])
        self.assertGreater(archived['archive_id'],0)
        records=[kw for method,kw in self.api.calls if method=='sendMessage' and kw['chat_id']==CH]
        self.assertIn('未实际处罚',records[-1]['text'])
        self.assertFalse(records[-1]['disable_notification'])
        self.assertNotIn('adcase:unban',str(records[-1]['reply_markup']))
        self.assertNotIn('banChatMember',self.methods())
    def test_dry_admin_reject_also_archived(self):
        c=self.create();self.callback(c,'reject')
        self.assertGreater(self.cases.get(c['id'])['archive_id'],0)
    def test_dry_archive_failure_preserves_simulated_result(self):
        c=self.create();self.api.fail='copyMessage';self.callback(c,'ban')
        self.assertEqual(self.cases.get(c['id'])['state'],'simulated_ban')
        self.assertNotIn('banChatMember',self.methods())

if __name__=='__main__':unittest.main()
