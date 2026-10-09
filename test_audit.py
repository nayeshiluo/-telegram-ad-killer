import socket
import sqlite3
import unittest
from unittest.mock import patch
from bot import APIError
from cases import Cases,message_key
import test_management as fixtures
from test_cases import sample,CID,CH
from transport import connect

class AuditTests(unittest.TestCase):
    setUp=fixtures.ManagementTests.setUp
    tearDown=fixtures.ManagementTests.tearDown
    cmd=fixtures.ManagementTests.cmd
    calls=fixtures.ManagementTests.calls
    banned=fixtures.ManagementTests.banned
    def test_review_command_has_real_three_minute_deadline(self):
        policy=self.store.policy(CID);policy['mode']='review';self.store.set('group:'+str(CID),policy)
        self.cmd('/adreview',reply_to_message=sample())
        c=self.bot.cases.get(1);self.assertEqual(c['deadline']-c['created'],180);self.assertEqual(c['dry'],0)
    def test_promoting_dry_case_gets_new_evidence_and_clears_votes(self):
        c=self.bot.cases.open(sample(),'old',dry=True,announce=False)
        self.bot.cases.execute(c['id'],'timeout');old=self.bot.cases.get(c['id'])['archive_id']
        self.store.db.execute('insert into case_votes values(?,?)',(c['id'],3));self.store.db.commit()
        self.cmd('/adkill CONFIRM',reply_to_message=sample())
        c=self.bot.cases.get(c['id']);self.assertNotEqual(c['archive_id'],old)
        self.assertIn('处罚记录',c['archive_text']);self.assertNotIn('观察测试',c['archive_text'])
        self.assertEqual(self.store.db.execute('select count(*) from case_votes').fetchone()[0],0)
    def test_restart_does_not_replay_interrupted_actions(self):
        for index,state in enumerate(('preparing','archiving','delete_pending','ban_pending','unban_pending')):
            c=self.bot.cases.open({**sample(),'message_id':20+index},'crash',dry=False,announce=False)
            self.bot.cases.set_state(c['id'],state)
        self.api.calls.clear();recovered=Cases(self.bot,CH)
        states=[r[0] for r in self.store.db.execute('select state from cases order by id')]
        self.assertEqual(states,['review_post_failed','archiving_failed','delete_pending_failed','ban_pending_failed','unban_failed'])
        self.assertEqual(self.api.calls,[])
    def test_interrupted_ban_can_be_unbanned_after_restart(self):
        c=self.bot.cases.open(sample(),'crash',dry=False,announce=False)
        self.bot.cases.set_state(c['id'],'ban_pending');self.bot.cases=Cases(self.bot,CH)
        self.cmd('/adunban '+self.bot.cases.number(c['id']))
        self.assertEqual(len(self.calls('unbanChatMember')),1)
    def test_old_unbanned_case_cannot_release_new_uncertain_ban(self):
        old=self.banned();self.cmd('/adunban '+self.bot.cases.number(old['id']))
        new=self.bot.cases.open({**sample(),'message_id':8},'new',dry=False,announce=False)
        self.bot.cases.set_state(new['id'],'ban_pending_failed');self.api.calls.clear()
        self.cmd('/adwrong '+self.bot.cases.number(old['id']))
        self.assertEqual(len(self.calls('unbanChatMember')),0)
    def test_event_log_failure_does_not_claim_successful_ban_failed(self):
        self.store.event=lambda *a:(_ for _ in ()).throw(RuntimeError('event failed'))
        c=self.banned();self.assertEqual(c['state'],'banned')
        self.assertEqual(self.store.db.execute('select active from active_bans').fetchone()[0],1)
    def test_metadata_failure_rolls_back_state_and_blacklist_together(self):
        self.store.db.execute("CREATE TRIGGER fail_id BEFORE INSERT ON active_bans BEGIN SELECT RAISE(ABORT,'fail'); END")
        c=self.banned();self.assertEqual(c['state'],'ban_pending_failed')
        self.assertEqual(self.store.db.execute('select count(*) from case_samples').fetchone()[0],0)
    def test_scoped_status_does_not_count_other_groups(self):
        self.store.db.execute('insert into active_bans values(?,?,?,1)',(-1001234567890,2,1));self.store.db.commit()
        self.assertIn('有效ID记录：0',self.bot.cases.status(CID));self.assertIn('有效ID记录：1',self.bot.cases.status())
    def test_cancelled_cases_ui_refreshed_on_unban(self):
        c=self.banned();pending=self.bot.cases.open({**sample(),'message_id':8},'pending',dry=False)
        self.api.calls.clear();self.cmd('/adunban '+self.bot.cases.number(c['id']))
        updates=self.calls('editMessageText');self.assertTrue(any(p.get('message_id')==pending['report_id'] for p in updates))
    def test_stale_image_ocr_is_not_learned_on_manual_confirmation(self):
        self.store.db.execute('create table ai_reviews(chat_id,message_id,label,observed_text,digest)')
        image={**sample(text=''),'photo':[{'file_id':'new'}]}
        self.store.db.execute('insert into ai_reviews values(?,?,?,?,?)',(CID,7,'spam','旧图片广告现货出售批发联系客服立即购买','old_digest'));self.store.db.commit()
        self.bot.ai=object();self.cmd('/adkill CONFIRM',reply_to_message=image)
        self.assertEqual(self.store.db.execute('select count(*) from case_samples').fetchone()[0],0)
    def test_current_image_ocr_can_be_learned_when_confirmed(self):
        self.store.db.execute('create table ai_reviews(chat_id,message_id,label,observed_text,digest)')
        image={**sample(text=''),'photo':[{'file_id':'new'}]}
        self.store.db.execute('insert into ai_reviews values(?,?,?,?,?)',(CID,7,'spam','当前图片广告现货出售批发联系客服立即购买下单优惠',message_key(image)));self.store.db.commit()
        self.bot.ai=object();self.cmd('/adkill CONFIRM',reply_to_message=image)
        self.assertEqual(self.store.db.execute('select count(*) from case_samples').fetchone()[0],1)
    def test_delete_mode_does_not_require_ban_permission(self):
        original=self.api.call
        def call(method,**kw):
            result=original(method,**kw)
            if method=='getChatMember' and kw['user_id']==99:result['can_restrict_members']=False
            return result
        self.api.call=call
        c=self.bot.cases.open(sample(),'delete only',dry=False,announce=False)
        self.bot.cases.execute(c['id'],'explicit_domain',ban=False)
        self.assertEqual(self.bot.cases.get(c['id'])['state'],'deleted')
    def test_repeat_unban_does_not_repeat_api_call(self):
        c=self.banned();name=self.bot.cases.number(c['id'])
        self.cmd('/adunban '+name);self.cmd('/adunban '+name)
        self.assertEqual(len(self.calls('unbanChatMember')),1)
    def test_failed_whitelist_audit_rolls_back_white_entry(self):
        self.store.db.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON management_audit BEGIN SELECT RAISE(ABORT,'fail'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.cmd('/adwhite add 2',uid=1)
        self.store.set('offset',100)
        self.assertFalse(self.m.whitelisted(CID,2))
    def test_unban_metadata_failure_does_not_partially_clear_blacklist(self):
        c=self.banned()
        self.store.db.execute("CREATE TRIGGER fail_sample BEFORE UPDATE ON case_samples BEGIN SELECT RAISE(ABORT,'fail'); END")
        self.cmd('/adwrong '+self.bot.cases.number(c['id']))
        self.assertEqual(self.bot.cases.get(c['id'])['state'],'unban_failed')
        self.assertEqual(self.store.db.execute('select active from active_bans').fetchone()[0],1)

class TransportTests(unittest.TestCase):
    def addresses(self):return [(socket.AF_INET6,socket.SOCK_STREAM,6,'',('::1',443,0,0)),(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]
    def test_ipv4_preferred_even_when_dns_returns_ipv6_first(self):
        order=[]
        class Fake:
            def __init__(self,family,*args):self.family=family
            def settimeout(self,t):pass
            def connect(self,a):order.append(self.family)
        with patch('transport.socket.getaddrinfo',return_value=self.addresses()),patch('transport.socket.socket',Fake):connect(('api.telegram.org',443),8)
        self.assertEqual(order,[socket.AF_INET])
    def test_ipv6_fallback_if_ipv4_fails(self):
        order=[];closed=[]
        class Fake:
            def __init__(self,family,*args):self.family=family
            def settimeout(self,t):pass
            def connect(self,a):
                order.append(self.family)
                if self.family==socket.AF_INET:raise OSError('IPv4 failed')
            def close(self):closed.append(self.family)
        with patch('transport.socket.getaddrinfo',return_value=self.addresses()),patch('transport.socket.socket',Fake):connect(('api.telegram.org',443),8)
        self.assertEqual(order,[socket.AF_INET,socket.AF_INET6]);self.assertEqual(closed,[socket.AF_INET])
    def test_connection_timeout_budget_is_shared(self):
        timeouts=[]
        class Fake:
            def __init__(self,*a):pass
            def settimeout(self,t):timeouts.append(t)
            def connect(self,a):raise OSError('failed')
            def close(self):pass
        with patch('transport.socket.getaddrinfo',return_value=self.addresses()),patch('transport.socket.socket',Fake),patch('transport.time.monotonic',side_effect=[0,1,4]):
            with self.assertRaises(OSError):connect(('api.telegram.org',443),8)
        self.assertEqual(timeouts,[7,4])
    def test_https_handler_retains_default_verified_context(self):
        from transport import HTTPSHandler,HTTPSConnection
        h=HTTPSHandler()
        with patch.object(h,'do_open',return_value='ok') as opened:
            self.assertEqual(h.https_open('request'),'ok')
        opened.assert_called_once_with(HTTPSConnection,'request',context=h._context)

if __name__=='__main__':unittest.main()
