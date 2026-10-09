"""Follow-up probes: tags cannot erase explicit ad signals or bypass image redirects."""
import io
import unittest
import urllib.request
import urllib.response
from email.message import Message
from unittest.mock import patch
from ai import AIClient,ReviewError
from rules import classify
from policy import verdict
from test_cases import CID,sample
import test_quarantine as fixture

class RuleFollowup(unittest.TestCase):
    def test_bot_link_ad_survives_antifraud_tags(self):
        for tag in ('',' 反诈宣传',' 注意防骗',' 不是骗局',' 怎么样？'):
            self.assertEqual(classify(sample(text='免费领取虚拟卡 t.me/somebot?start=x'+tag),{})['reason'],'bot_promotion')

    def test_bot_invitation_prefix_is_not_an_exemption(self):
        for prefix in ('提醒：','反诈宣传：','注意：','谨防：'):
            self.assertEqual(classify(sample(text=prefix+'免费领取虚拟卡 t.me/somebot?start=x'),{})['level'],'suspected')

    def test_mention_and_inline_bot_ad_tags(self):
        for tag in (' 反诈宣传','？'):
            for m in (sample(text='@somebot 免费领取虚拟卡'+tag),
                      {**sample(text='免费领取'+tag),'via_bot':{'is_bot':True,'username':'somebot'}},
                      {**sample(text='免费领取'+tag),'entities':[{'type':'text_link','url':'https://t.me/somebot?start=x'}]}):
                self.assertEqual(classify(m,{})['reason'],'bot_promotion')

    def test_wechat_recruitment_with_or_without_question(self):
        for contact in ('加微信 abc123','添加微信 abc123','加薇信 abc123','加我微信 abc123'):
            for tag in ('','？',' 反诈宣传',' 不是骗局'):
                self.assertEqual(classify(sample(text=contact+' 稳赚不赔，兼职日结'+tag),{})['reason'],'promotion_and_contact')

    def test_explicit_offer_is_not_erased_by_forged_prefix(self):
        for text in ('提醒：招代理 联系 @fakeuser 日赚500 注意防骗',
                     '提醒：虚拟卡现货批发，联系客服 不是骗局',
                     '虚拟卡免费开卡保证通过 联系客服 不是骗局',
                     '虚拟卡现货批发 反诈宣传'):
            self.assertEqual(classify(sample(text=text),{})['level'],'suspected',text)

    def test_contact_and_weak_warning_alone_still_clean(self):
        for text in ('加微信 abc123 明天讨论技术','我微信号改了','@somebot 这个怎么用？',
                     '反诈宣传：不要信兼职日结稳赚骗局','谨防VCC广告，100%能过0开卡是骗局',
                     '服务器购买价格怎么样？'):
            self.assertEqual(classify(sample(text=text),{})['level'],'clean',text)

    def test_bot_switch_still_excludes_bot_rule(self):
        m=sample(text='免费领取福利 t.me/somebot?start=x 反诈宣传')
        self.assertEqual(verdict(m,{'features':{'bot':False}})['level'],'clean')

class ReviewContextFollowup(unittest.TestCase):
    setUp=fixture.QuarantineTests.setUp
    tearDown=fixture.QuarantineTests.tearDown
    def test_quoted_warning_with_invitation_requires_context_not_automatic_punishment(self):
        m=sample(text='谨防兼职日结稳赚 私聊加我这样的骗局')
        self.assertEqual(classify(m,{})['level'],'suspected')
        self.cases.track(m)
        self.cases.ai_result(m,{'label':'normal','reason':'反诈引用讨论','observed_text':''})
        self.assertEqual(self.store.db.execute('select count(*) from cases').fetchone()[0],0)
        self.assertFalse(any(n in {'deleteMessage','restrictChatMember','banChatMember'} for n,p in self.api.calls))

    def test_ambiguous_warning_without_ai_is_manual_review_only(self):
        self.bot.ai=None
        m=sample(text='提醒：免费领取虚拟卡 t.me/somebot?start=x 反诈宣传')
        self.cases.consider(m)
        self.assertEqual(self.cases.get(1)['state'],'pending')
        self.assertFalse(any(n in {'deleteMessage','restrictChatMember','banChatMember'} for n,p in self.api.calls))

class ImageRedirectFollowup(unittest.TestCase):
    def test_image_uses_secure_opener_including_custom_api_open(self):
        for custom in (False,True):
            visits=[]
            class RedirectHTTPS(urllib.request.HTTPSHandler):
                def https_open(self,req):
                    visits.append(req.full_url)
                    headers=Message();headers['Location']='https://redirect.invalid/image'
                    response=urllib.response.addinfourl(io.BytesIO(b''),headers,req.full_url,302)
                    response.msg='Found'
                    return response
            class API:
                token='synthetic-image-token'
                def call(self,*a,**kw):return {'file_path':'photos/image.jpg'}
            api=API()
            if custom:api._open=lambda *a,**kw:(_ for _ in ()).throw(AssertionError('insecure API opener used'))
            with patch('ai.HTTPSHandler',return_value=RedirectHTTPS()):
                client=AIClient({'base_url':'https://model.invalid/v1'},'synthetic')
            with self.assertRaises(ReviewError):client.image(api,{'photo':[{'file_id':'image','width':640,'height':480}]})
            self.assertEqual(len(visits),1)
            self.assertNotIn('redirect.invalid',visits[0])

    def test_image_success_still_validates_size_and_format(self):
        class API:
            token='synthetic-image-token'
            def call(self,*a,**kw):return {'file_path':'photos/image.png'}
        client=AIClient({'base_url':'https://model.invalid/v1'},'synthetic')
        client._open=lambda *a,**kw:io.BytesIO(b'\x89PNG\r\n\x1a\nsynthetic')
        self.assertTrue(client.image(API(),{'photo':[{'file_id':'image'}]}).startswith('data:image/png;base64,'))
        client._open=lambda *a,**kw:io.BytesIO(b'<html>')
        with self.assertRaises(ReviewError):client.image(API(),{'photo':[{'file_id':'image'}]})
