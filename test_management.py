import tempfile
import unittest
from pathlib import Path
from bot import Bot,Store,APIError
from test_cases import API,CID,CH,sample

OTHER=-1001234567890

class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        cfg={'owner_id':1,'archive_channel':CH,'groups':{str(CID):{'mode':'observe','blocked_domains':[]},str(OTHER):{'mode':'review','blocked_domains':[]}}}
        self.store=Store(Path(self.temp.name)/'db',cfg);self.api=API()
        self.bot=Bot(self.api,self.store,{'id':99,'username':'test_bot'},1);self.m=self.bot.management
    def tearDown(self):self.store.db.close();self.temp.cleanup()
    def cmd(self,text,uid=20,cid=CID,private=False,**extra):
        m={**sample(uid=uid,text=text),**extra,'chat':{'id':cid,'type':'private' if private else 'supergroup'}}
        self.bot.handle({'message':m})
    def calls(self,method):return [kw for name,kw in self.api.calls if name==method]
    def banned(self,cid=CID):
        c=self.bot.cases.open({**sample(),'chat':{'id':cid,'type':'supergroup'}},'真实确认测试',dry=False,announce=False)
        self.bot.cases.execute(c['id'],'admin:20');return self.bot.cases.get(c['id'])
    def panel_callback(self,action,uid=20,where=CID,mid=None):
        p=self.store.db.execute('SELECT * FROM management_panels ORDER BY rowid DESC LIMIT 1').fetchone()
        self.bot.handle({'callback_query':{'id':'q','from':{'id':uid},'data':'adm:'+p['token']+':'+action,
            'message':{'chat':{'id':where},'message_id':mid if mid is not None else p['mid']}}})
    def test_group_white_does_not_protect_other_group(self):
        self.cmd('/adwhite add 2');self.assertTrue(self.m.whitelisted(CID,2));self.assertFalse(self.m.whitelisted(OTHER,2))
    def test_owner_global_white_protects_all_groups(self):
        self.cmd('/adgwhite add 2',uid=1);self.assertTrue(self.m.whitelisted(OTHER,2))
    def test_group_admin_cannot_change_global_white(self):
        self.cmd('/adgwhite add 2');self.assertFalse(self.m.whitelisted(CID,2))
    def test_ordinary_member_cannot_change_white(self):
        self.cmd('/adwhite add 2',uid=3);self.assertFalse(self.m.whitelisted(CID,2))
    def test_removing_local_does_not_remove_global(self):
        self.cmd('/adgwhite add 2',uid=1);self.cmd('/adwhite add 2');self.cmd('/adwhite remove 2')
        self.assertTrue(self.m.whitelisted(CID,2));self.assertIn('全局白名单仍生效',self.calls('sendMessage')[-1]['text'])
    def test_white_does_not_grant_command_access(self):
        self.cmd('/adwhite add 3');self.cmd('/adwhite add 4',uid=3);self.assertFalse(self.m.whitelisted(CID,4))
    def test_white_skips_ai_and_rules_and_cases(self):
        class AI:
            def submit(self,m):raise AssertionError('white message must skip AI')
        self.cmd('/adwhite add 2');self.bot.ai=AI();self.cmd('出售优惠广告 联系客服',uid=2)
        self.assertEqual(self.store.db.execute('select count(*) from cases').fetchone()[0],0)
    def test_white_cannot_be_manually_killed(self):
        self.cmd('/adwhite add 2');self.cmd('/adkill CONFIRM',reply_to_message=sample())
        self.assertEqual(len(self.calls('banChatMember')),0)
    def test_white_cancels_real_pending_case(self):
        c=self.bot.cases.open(sample(),'pending',dry=False,announce=False)
        self.cmd('/adwhite add 2');self.assertEqual(self.bot.cases.get(c['id'])['state'],'whitelisted')
    def test_blacklisted_member_must_be_unbanned_first(self):
        self.banned();self.cmd('/adwhite add 2');self.assertFalse(self.m.whitelisted(CID,2))
    def test_admin_unban_deactivates_blacklist_preserves_samples(self):
        c=self.banned();self.cmd('/adunban '+self.bot.cases.number(c['id']))
        self.assertEqual(self.bot.cases.get(c['id'])['state'],'unbanned')
        self.assertEqual(self.store.db.execute('select active from active_bans').fetchone()[0],0)
        self.assertEqual(self.store.db.execute('select active from case_samples').fetchone()[0],1)
    def test_wrong_retracts_learning_and_records_actor(self):
        c=self.banned();self.cmd('/adwrong '+self.bot.cases.number(c['id']))
        self.assertEqual(self.store.db.execute('select active from case_samples').fetchone()[0],0)
        audit=self.store.db.execute('select actor,action from management_audit order by id desc limit 1').fetchone()
        self.assertEqual(tuple(audit),(20,'wrong'))
    def test_unban_failure_does_not_clear_blacklist(self):
        c=self.banned();self.api.fail='unbanChatMember';self.cmd('/adwrong '+self.bot.cases.number(c['id']))
        self.assertEqual(self.store.db.execute('select active from active_bans').fetchone()[0],1)
        self.assertEqual(self.store.db.execute('select active from case_samples').fetchone()[0],1)
    def test_cross_group_case_cannot_be_unbanned(self):
        c=self.banned(OTHER);self.cmd('/adwrong '+self.bot.cases.number(c['id']))
        self.assertEqual(len(self.calls('unbanChatMember')),0)
    def test_dry_case_never_unbanned(self):
        c=self.bot.cases.open(sample(),'dry',dry=True);self.cmd('/adunban '+self.bot.cases.number(c['id']))
        self.assertEqual(len(self.calls('unbanChatMember')),0)
    def test_blacklist_scope_and_pagination(self):
        for n in range(9):self.store.db.execute('insert into active_bans values(?,?,?,1)',(CID,100+n,n+1))
        self.store.db.execute('insert into active_bans values(?,?,?,1)',(OTHER,555,50));self.store.db.commit()
        text,more,_=self.m.listing(CID,'black');self.assertTrue(more);self.assertNotIn('555',text)
        text,more,_=self.m.listing(CID,'black',2);self.assertFalse(more);self.assertIn('用户ID：100',text)
    def test_history_filter_excludes_other_member(self):
        self.bot.cases.open(sample(),'x',dry=True,announce=False)
        self.cmd('/adhistory 3');self.assertIn('没有记录',self.calls('sendMessage')[-1]['text'])
    def test_forged_forwarded_panel_rejected(self):
        c=self.banned();self.cmd('/admanage');self.panel_callback('wrong:'+str(c['id']),mid=999)
        self.assertEqual(len(self.calls('unbanChatMember')),0)
    def test_ordinary_member_panel_rejected(self):
        c=self.banned();self.cmd('/admanage');self.panel_callback('wrong:'+str(c['id']),uid=3)
        self.assertEqual(len(self.calls('unbanChatMember')),0)
    def test_admin_promotion_revoked_panel_permission(self):
        c=self.banned();self.cmd('/admanage');self.api.roles[20]='member';self.panel_callback('wrong:'+str(c['id']))
        self.assertEqual(len(self.calls('unbanChatMember')),0)
    def test_original_panel_can_correct_case(self):
        c=self.banned();self.cmd('/admanage');self.panel_callback('wrong:'+str(c['id']))
        self.assertEqual(self.bot.cases.get(c['id'])['state'],'wrong_unbanned')
    def test_private_owner_selects_authorized_group_only(self):
        self.cmd('/admanage '+str(CID),uid=1,cid=1,private=True)
        self.assertEqual(self.store.db.execute('select cid from management_panels').fetchone()[0],CID)
        self.cmd('/admanage -100999',uid=1,cid=1,private=True)
        self.assertEqual(self.store.db.execute('select count(*) from management_panels').fetchone()[0],1)
    def test_nonowner_private_cannot_change_global(self):
        self.cmd('/adgwhite add 2',uid=20,cid=20,private=True);self.assertFalse(self.m.whitelisted(CID,2))
    def test_private_owner_global_white(self):
        self.cmd('/adgwhite add 2',uid=1,cid=1,private=True);self.assertTrue(self.m.whitelisted(OTHER,2))
    def test_whitelist_change_history_keeps_scope_and_actor(self):
        self.cmd('/adwhite add 2');self.cmd('/adwhite remove 2')
        rows=list(map(tuple,self.store.db.execute('select scope,actor,action,target from management_audit')))
        self.assertEqual(rows,[(CID,20,'white_add',2),(CID,20,'white_remove',2)])
    def test_menu_callback_is_expired_after_one_day(self):
        self.cmd('/admanage');self.store.db.execute('update management_panels set created=0');self.store.db.commit()
        self.panel_callback('history:1');self.assertEqual(len(self.calls('editMessageText')),0)

if __name__=='__main__':unittest.main()
