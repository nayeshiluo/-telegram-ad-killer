import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_cases import API, sample, CID, CH
from bot import Bot, Store
from policy import Repeats, verdict, features
from ai import ReviewWorker

class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'db',{'owner_id':1,'archive_channel':CH,'groups':{str(CID):{'mode':'observe','blocked_domains':[]}}})
        self.api=API();self.bot=Bot(self.api,self.store,{'id':99,'username':'test_bot'},1)
    def tearDown(self):self.store.db.close();self.temp.cleanup()
    def test_screenshot_ad_and_normal_discussion(self):
        self.assertEqual(verdict({'text':'抖音代刷 一天9千'}, {})['reason'],'paid_boost_promotion')
        for text in ('抖音代刷一天9千是真的吗？','谨防抖音代刷一天9千骗局','今天讨论抖音播放量'):
            self.assertEqual(verdict({'text':text}, {})['level'],'clean')
    def test_repeat_requires_three_distinct_messages_and_group_scope(self):
        r=Repeats();p={};m=sample();v=verdict(m,p)
        self.assertIsNone(r.check(m,p,v));self.assertIsNone(r.check(m,p,v))
        self.assertIsNone(r.check({**m,'message_id':8},p,v))
        self.assertEqual(r.check({**m,'message_id':9},p,v)['reason'],'repeated_promotion')
        self.assertIsNone(r.check({**m,'chat':{'id':-2},'message_id':10},p,v))
    def test_repeat_expiry_and_clean_repeats(self):
        r=Repeats();m=sample();v=verdict(m,{})
        with patch('policy.time.monotonic',return_value=0):r.check(m,{},v)
        with patch('policy.time.monotonic',return_value=121):
            self.assertIsNone(r.check({**m,'message_id':8},{},v))
        self.assertIsNone(r.check(m,{}, {'level':'clean'}))
    def test_switches_keep_explicit_domains(self):
        p={'features':{k:False for k in features({})},'blocked_domains':['spam.test']}
        self.assertEqual(verdict({'text':'https://spam.test'},p)['level'],'confirmed')
        self.assertEqual(verdict(sample(),p)['level'],'clean')
    def test_bot_switch_removes_bot_specific_reason(self):
        self.assertEqual(verdict({'text':'@demoad_bot 免费领取福利'}, {})['reason'],'bot_promotion')
        self.assertEqual(verdict({'text':'@demoad_bot 免费领取福利'}, {'features':{'bot':False}})['level'],'clean')
    def test_only_self_can_appeal_once_and_no_unban(self):
        c=self.bot.cases.open(sample(),'测试',dry=False,announce=False)
        self.bot.cases.set_state(c['id'],'banned')
        number=self.bot.cases.number(c['id'])
        m={'chat':{'id':3,'type':'private'},'message_id':77,'from':{'id':3},'text':'/adappeal '+number+' 正常讨论'}
        self.bot.private(m)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM appeals').fetchone()[0],0)
        m['from']['id']=2;m['chat']['id']=2
        self.bot.private(m);self.bot.private(m)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM appeals').fetchone()[0],1)
        self.assertNotIn('unbanChatMember',[x[0] for x in self.api.calls])
    def test_setting_button_scope_and_permissions(self):
        m=sample(uid=20,text='/adsettings');self.bot.handle({'message':m})
        posted=[p for n,p in self.api.calls if n=='sendMessage'][-1]
        token=self.store.db.execute('SELECT token,mid FROM management_panels').fetchone()
        q={'id':'q','data':'adm:'+token[0]+':toggle:0','from':{'id':2},'message':{'chat':{'id':CID},'message_id':token[1]}}
        self.bot.handle({'callback_query':q});self.assertTrue(features(self.store.policy(CID))['text'])
        q['from']['id']=20;self.bot.handle({'callback_query':q})
        self.assertFalse(features(self.store.policy(CID))['text'])
        self.assertEqual(self.store.policy(CID)['mode'],'observe')
    def test_ai_results_after_setting_change_do_not_open_case(self):
        p=self.store.policy(CID);p['mode']='review';self.store.set('group:'+str(CID),p)
        m={**sample(), '_ad_features':features(p)};self.bot.cases.track(m)
        p['features']={'photo':False};self.store.set('group:'+str(CID),p)
        self.bot.cases.ai_result(m,{'label':'spam','reason':'广告','observed_text':''})
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM cases').fetchone()[0],0)

class CacheTests(unittest.TestCase):
    def test_cache_scoped_and_manual_bypass_and_features(self):
        with tempfile.TemporaryDirectory() as temp:
            api=API();api.enqueue_send=lambda **kw:None
            calls=[]
            client=type('Client',(),{'config':{'model':'test'}})()
            client.review=lambda *a:calls.append(1) or {'label':'spam','reason':'广告','observed_text':''}
            worker=ReviewWorker(client,api,Path(temp)/'db',1);worker.close()
            m=sample();worker.process(m,None)
            worker.process({**m,'message_id':8},None)
            self.assertEqual(len(calls),1);self.assertEqual(worker.cache_hits,1)
            worker.process({**m,'message_id':9},m)
            worker.process({**m,'chat':{'id':-222},'message_id':10},None)
            worker.process({**m,'message_id':11,'_ad_features':{'photo':False}},None)
            self.assertEqual(len(calls),4)
            client.config['model']='other';worker.process({**m,'message_id':12},None)
            self.assertEqual(len(calls),5)

if __name__=='__main__':unittest.main()
