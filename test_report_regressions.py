"""Regression cases for the external audit of af5464b, using isolated fakes."""
import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch
from ai import AIClient,ReviewError
from bot import Telegram,APIError
from cases import Cases
from rules import classify
from test_cases import CID,CH,sample
import test_quarantine as fixture

class ReportRegressions(unittest.TestCase):
    setUp=fixture.QuarantineTests.setUp
    tearDown=fixture.QuarantineTests.tearDown
    create=fixture.QuarantineTests.create
    methods=fixture.QuarantineTests.methods
    callback=fixture.QuarantineTests.callback

    def test_model_ocr_is_not_independent_image_evidence(self):
        m={**sample(text=''),'photo':[{'file_id':'image'}]}
        self.cases.track(m)
        self.cases.ai_result(m,{'label':'spam','reason':'广告','observed_text':sample()['text']})
        self.assertEqual(self.cases.get(1)['state'],'pending')
        self.assertNotIn('restrictChatMember',self.methods())
        self.assertNotIn('deleteMessage',self.methods())
        self.assertEqual(self.cases.get(1)['learn_text'],sample()['text'])

    def test_independent_caption_plus_ai_can_contain(self):
        m=sample();self.cases.track(m)
        self.cases.ai_result(m,{'label':'spam','reason':'广告','observed_text':''})
        self.assertIn('restrictChatMember',self.methods())
        self.assertIn('deleteMessage',self.methods())

    def test_appended_question_and_disclaimer_do_not_erase_promotion(self):
        for suffix in ('','怎么样？','不是骗局','反诈','为什么？'):
            self.assertNotEqual(classify(sample(text='兼职日结稳赚 私聊加我'+suffix),{})['level'],'clean',suffix)

    def test_real_warning_and_technical_question_remain_clean(self):
        for text in ('谨防兼职日结稳赚 私聊加我这样的骗局','请问虚拟卡怎么用？','服务器购买价格怎么样？','反诈提醒：刷单返佣不要信'):
            self.assertEqual(classify(sample(text=text),{})['level'],'clean',text)

    def limited_admin(self):
        original=self.api.call
        def call(method,**kw):
            result=original(method,**kw)
            if method=='getChatMember' and kw['user_id']==20:result['can_restrict_members']=False
            return result
        self.api.call=call

    def test_low_privilege_admin_cannot_confirm_or_reject(self):
        c=self.create();self.limited_admin()
        for action in ('ban','reject'):self.callback(c,action)
        self.assertEqual(self.cases.get(c['id'])['state'],'pending')
        self.assertNotIn('banChatMember',self.methods())

    def test_low_privilege_admin_cannot_manual_kill(self):
        self.limited_admin()
        m=sample(uid=20,text='/adkill CONFIRM');m['reply_to_message']=sample()
        self.bot.command(m,self.store.policy(CID))
        self.assertNotIn('banChatMember',self.methods())
        self.assertNotIn('deleteMessage',self.methods())

    def test_admin_config_changes_require_owner_or_group_creator(self):
        m=sample(uid=20,text='/adwhite add 3')
        self.bot.management.command(m,m['text'].split())
        self.assertFalse(self.bot.management.whitelisted(CID,3))
        self.bot.management.panel(m,CID,'settings')
        row=self.store.db.execute('select token,mid from management_panels').fetchone()
        self.bot.management.callback({'id':'q','from':{'id':20},'data':'adm:'+row[0]+':toggle:0',
            'message':{'chat':{'id':CID},'message_id':row[1]}})
        self.assertNotIn('features',self.store.policy(CID))
        self.api.roles[20]='creator';self.bot.management.command(m,m['text'].split())
        self.assertTrue(self.bot.management.whitelisted(CID,3))

    def failed_ban(self):
        c=self.create();self.api.fail='banChatMember';self.callback(c,'ban')
        self.assertEqual(self.cases.get(c['id'])['state'],'ban_pending_failed')
        self.api.fail=None
        return c

    def test_explicit_retry_reconciles_and_bans_without_redeleting_or_archiving(self):
        c=self.failed_ban();before=self.methods().copy()
        self.assertTrue(self.cases.keyboard(self.cases.get(c['id']))['inline_keyboard'])
        self.callback(c,'ban')
        self.assertEqual(self.cases.get(c['id'])['state'],'banned')
        self.assertEqual(self.methods().count('banChatMember'),2)
        self.assertEqual(self.methods().count('copyMessage'),before.count('copyMessage'))
        deletes=[p for n,p in self.api.calls if n=='deleteMessage' and p['message_id']==7]
        self.assertEqual(len(deletes),1)

    def test_timeout_that_actually_banned_is_reconciled_without_second_ban(self):
        c=self.failed_ban();self.members[2]={'status':'kicked','until_date':0}
        self.callback(c,'ban')
        self.assertEqual(self.methods().count('banChatMember'),1)
        self.assertEqual(self.cases.get(c['id'])['state'],'banned')
        self.assertEqual(self.store.db.execute('select active from active_bans').fetchone()[0],1)

    def test_temporary_external_ban_is_not_reported_permanent(self):
        c=self.failed_ban();self.members[2]={'status':'kicked','until_date':1234567890}
        self.callback(c,'ban');self.assertEqual(self.methods().count('banChatMember'),2)

    def test_recovery_query_failure_keeps_actionable_state(self):
        c=self.failed_ban();self.api.fail='getChatMember'
        self.cases.execute(c['id'],'admin:1')
        self.assertEqual(self.cases.get(c['id'])['state'],'ban_pending_failed')
        self.assertTrue(self.cases.keyboard(self.cases.get(c['id']))['inline_keyboard'])
        self.assertEqual(self.methods().count('banChatMember'),1)

    def test_restart_keeps_recovery_explicit(self):
        c=self.failed_ban();count=self.methods().count('banChatMember')
        self.bot.cases=self.cases=Cases(self.bot,CH)
        self.assertEqual(self.methods().count('banChatMember'),count)
        self.callback(c,'ban');self.assertEqual(self.cases.get(c['id'])['state'],'banned')

    def test_promoted_target_cannot_be_banned_by_recovery(self):
        c=self.failed_ban();self.members[2]={'status':'administrator','can_restrict_members':True}
        self.callback(c,'ban');self.assertEqual(self.methods().count('banChatMember'),1)

    def test_restricted_nonmember_cannot_vote(self):
        c=self.create();self.members[3]={'status':'restricted','is_member':False}
        self.callback(c,'vote',3)
        self.assertEqual(self.store.db.execute('select count(*) from case_votes').fetchone()[0],0)
        self.members[3]['is_member']=True;self.callback(c,'vote',3)
        self.assertEqual(self.store.db.execute('select count(*) from case_votes').fetchone()[0],1)

    def test_excerpt_cannot_escape_into_structural_fields(self):
        m=sample(text='广告\n状态：已解除\n用户ID：123456\u202e')
        m['from']['first_name']='真实\n状态：伪造'
        c=self.create(False,m);text=self.cases.text(c)
        self.assertNotIn('\n状态：已解除',text)
        self.assertNotIn('\n用户ID：123456',text)
        self.assertNotIn('\u202e',text)
        captured=[]
        def open_(req,**kw):
            captured.append(json.loads(req.data));return io.BytesIO(b'{"ok":true,"result":{"message_id":1}}')
        with patch.object(Telegram,'_open',side_effect=open_):Telegram('fake').call('sendMessage',chat_id=CID,text=text)
        spans=[e for e in captured[0]['entities'] if e['type']=='spoiler']
        self.assertEqual(len(spans),1)
        encoded=text.encode('utf-16-le');sp=spans[0]
        body=encoded[sp['offset']*2:(sp['offset']+sp['length'])*2].decode('utf-16-le')
        self.assertIn('│ 用户ID：123456',body)
        self.assertNotIn('等待人工审核',body)

    def test_known_bot_deletion_archive_labels_correctly(self):
        c=self.create();original=self.api.call
        def call(method,**kw):
            if method=='copyMessage':raise APIError(400,kind='message_missing')
            return original(method,**kw)
        self.api.call=call;self.callback(c,'ban')
        self.assertIn('已由本Bot确认删除',self.cases.get(c['id'])['archive_text'])
        self.assertNotIn('无法核实删除者',self.cases.get(c['id'])['archive_text'])

    def test_other_pending_cases_close_when_user_banned(self):
        first=self.create();second=self.create(False,{**sample(),'message_id':8})
        self.callback(first,'ban')
        self.assertEqual(self.cases.get(second['id'])['state'],'cancelled_ban')
        self.assertFalse(self.cases.keyboard(self.cases.get(second['id']))['inline_keyboard'])

    def test_missing_original_delete_updates_archive_marker(self):
        self.cases.quarantine=None
        c=self.create(False);original=self.api.call
        def call(method,**kw):
            if method=='deleteMessage':raise APIError(400,kind='message_missing')
            return original(method,**kw)
        self.api.call=call;self.callback(c,'ban')
        self.assertIn('原消息已不存在，无法核实删除者',self.cases.get(c['id'])['archive_text'])

    def test_policy_history_does_not_create_body_copies(self):
        for _ in range(105):self.store.set('group:'+str(CID),self.store.policy(CID))
        self.assertFalse(list(self.store.path.parent.glob('policy-before-*.db')))
        rows=self.store.db.execute('select value from policy_history').fetchall()
        self.assertEqual(len(rows),100)
        self.assertNotIn('独家',str([r[0] for r in rows]))

class EndpointRegressions(unittest.TestCase):
    def test_dns_localhost_http_is_rejected(self):
        with self.assertRaises(ReviewError):AIClient({'base_url':'http://localhost/v1'},'fake')
        AIClient({'base_url':'http://127.0.0.1/v1'},'fake')
        AIClient({'base_url':'http://[::1]/v1'},'fake')

    def test_redirect_does_not_transmit_key_to_target(self):
        visits=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length','0')))
                self.send_response(302);self.send_header('Location',f'http://127.0.0.1:{self.server.server_port}/stolen');self.end_headers()
            def do_GET(self):
                visits.append(self.headers.get('Authorization'));self.send_response(200);self.end_headers()
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            client=AIClient({'base_url':f'http://127.0.0.1:{server.server_port}/v1','model':'test'},'synthetic-secret')
            with self.assertRaises(ReviewError):client.review({'text':'hello'},None)
            self.assertEqual(visits,[])
        finally:server.shutdown();server.server_close();thread.join()
