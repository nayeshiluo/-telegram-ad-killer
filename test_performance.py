import json
import os
import queue
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import bot
from test_cases import CID, sample
import test_quarantine


class PermissionRoundtripTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_quarantine.QuarantineTests();self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
    def test_containment_retains_three_fresh_target_checks(self):
        f=self.fixture;f.create()
        checks=[p for m,p in f.api.calls if m=='getChatMember']
        self.assertEqual(len(checks),5)
        self.assertEqual(sum(p['user_id']==2 for p in checks),3)
    def test_promotion_during_readiness_prevents_mute_and_delete(self):
        f=self.fixture;original=f.api.call
        def call(method,**kw):
            result=original(method,**kw)
            if method=='getChatMember' and kw['user_id']==99:
                f.members[2]={'status':'administrator'}
            return result
        f.api.call=call;f.create()
        self.assertNotIn('restrictChatMember',f.methods())
        self.assertNotIn('deleteMessage',f.methods())
    def test_promotion_during_mute_prevents_delete(self):
        f=self.fixture;original=f.api.call
        def call(method,**kw):
            result=original(method,**kw)
            if method=='restrictChatMember':f.members[2]={'status':'administrator'}
            return result
        f.api.call=call;f.create()
        self.assertNotIn('deleteMessage',f.methods())
    def test_management_command_one_lookup_and_no_cross_command_cache(self):
        f=self.fixture;m=sample(uid=20,text='/admanage')
        self.assertTrue(f.bot.command(m,f.store.policy(CID)))
        checks=[p for method,p in f.api.calls if method=='getChatMember' and p['user_id']==20]
        self.assertEqual(len(checks),1)
        f.api.roles[20]='member'
        self.assertFalse(f.bot.command({**m,'text':'/adwhite add 3'},f.store.policy(CID)))
        self.assertFalse(f.bot.management.whitelisted(CID,3))
    def test_missing_restrict_permission_cannot_open_manual_case(self):
        f=self.fixture;f.api.permissions=False
        self.assertTrue(f.bot.command(sample(uid=20,text='/adkill CONFIRM'),f.store.policy(CID)))
        self.assertNotIn('banChatMember',f.methods())
        self.assertEqual(f.store.db.execute('select count(*) from cases').fetchone()[0],0)


class MainSchedulingTests(unittest.TestCase):
    def test_callback_before_ai_and_only_one_result_per_poll(self):
        events=[];handlers={};results=queue.Queue()
        for i in range(20):results.put((i,{}))
        class API:
            def __init__(self,*args):pass
            def call(self,name,**kw):
                if name=='getMe':return {'id':99,'username':'test_bot'}
                if name=='getWebhookInfo':return {'url':''}
                if name=='getUpdates':
                    handlers[signal.SIGTERM]()
                    return [{'update_id':10,'callback_query':{'id':'test'}}]
                return True
            def start_outbox(self):pass
            def drain_outbox(self):pass
        class Worker:
            def __init__(self,*args):self.results=results
            def close(self):events.append('close')
        class Cases:
            def ai_result(self,*args):events.append('ai')
            def tick(self):events.append('tick')
        class Bot:
            def __init__(self,*args,**kw):self.cases=Cases()
            def handle(self,update):events.append('callback')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config={'expected_username':'test_bot','owner_id':1,'groups':{},'ai':{'enabled':True}}
            (root/'config.json').write_text(json.dumps(config))
            for name in ('bot-token','ai-key'):(root/name).write_text('synthetic-test-only')
            environment={'AD_CONFIG':str(root/'config.json'),'STATE_DIRECTORY':str(root/'state'),
                         'LOGS_DIRECTORY':str(root/'logs'),'CREDENTIALS_DIRECTORY':str(root)}
            before=list(bot.LOG.handlers)
            try:
                with patch.dict(os.environ,environment),patch('bot.Telegram',API),patch('bot.Bot',Bot),patch('ai.AIClient'),patch('ai.ReviewWorker',Worker),patch('bot.signal.signal',side_effect=lambda sig,fn:handlers.update({sig:fn})):
                    bot.main()
                self.assertEqual(events,['callback','ai','tick','close'])
                self.assertEqual(results.qsize(),19)
                self.assertEqual(results.unfinished_tasks,19)
                store=bot.Store(root/'state'/'state.db',config)
                try:self.assertEqual(store.get('offset'),11)
                finally:store.db.close()
            finally:
                for handler in list(bot.LOG.handlers):
                    if handler not in before:bot.LOG.removeHandler(handler);handler.close()

if __name__=='__main__':unittest.main()
