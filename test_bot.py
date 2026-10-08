import tempfile
import unittest
from pathlib import Path
from bot import Bot, Store, Telegram, APIError, command_menus
from rules import classify, domain, extract_links, explain
from unittest.mock import patch
import threading
import time
from rules import SAMPLES

CID = -1009000000001

def msg(text="hello", uid=2, **kwargs):
    return {"chat": {"id": CID, "type": "supergroup"}, "message_id": 7,
            "from": {"id": uid}, "text": text, **kwargs}

class FakeAPI:
    def __init__(self, status="member", fail=False):
        self.calls, self.status, self.fail = [], status, fail
    def call(self, method, **params):
        self.calls.append((method, params))
        if self.fail:
            raise APIError()
        if method == "getChatMember":
            return {"status": self.status, "can_delete_messages": True, "can_restrict_members": True}
        return True

class RulesTests(unittest.TestCase):
    def test_public_recruitment_excerpt_suspected(self):
        from rules import PUBLIC_REVIEW_SAMPLES
        self.assertEqual(classify(msg(PUBLIC_REVIEW_SAMPLES[0]),{})["reason"],"sample_similarity")
    def test_normal_recruitment_words_not_blanket_banned(self):
        for sample in ("昨晚干通宵调服务器，今天终于修好了", "兄弟们，下个月开车回家", "hy2搭配cdn的技术讨论"):
            self.assertEqual(classify(msg(sample),{})["level"],"clean")
    def test_selected_keyword_categories(self):
        for sample in ("微信号成品号现货批发，联系客服", "棋牌招代理高额返佣", "USDT换汇优惠联系我", "企业签名现货供应", "四件套出售", "社工库查档点头像", "高仿手表批发", "兼職日結加v客服"):
            with self.subTest(sample=sample):
                self.assertEqual(classify(msg(sample),{})["level"],"suspected")
    def test_normal_keyword_discussion(self):
        for sample in ("我微信号改了", "服务器内存不够", "企业签名怎么安装", "银行卡怎么办理", "USDT换汇失败怎么办", "今天买菠菜做饭", "谨防棋牌返佣骗局"):
            with self.subTest(sample=sample):
                self.assertEqual(classify(msg(sample),{})["level"],"clean")
    def test_bot_mention_needs_promotion(self):
        self.assertEqual(classify(msg("@ad_demo_bot 免费领取福利"),{})["reason"],"bot_promotion")
        for sample in ("@music_demo_bot 点歌", "@autoadblockerbot 这个怎么用？", "@ad_demo_bot", "联系 @normal_user"):
            self.assertEqual(classify(msg(sample),{})["level"],"clean")
    def test_deep_bot_link(self):
        self.assertEqual(classify(msg("领取红包 https://t.me/ad_demo_bot?start=123"),{})["reason"],"bot_promotion")
    def test_hidden_bot_link(self):
        m=msg("领取福利",entities=[{"type":"text_link","url":"https://t.me/ad_demo_bot?start=hello"}])
        self.assertEqual(classify(m,{})["reason"],"bot_promotion")
    def test_inline_bot_metadata(self):
        self.assertEqual(classify(msg("免费领取",via_bot={"is_bot":True,"username":"ad_demo_bot"}),{})["reason"],"bot_promotion")
        self.assertEqual(classify(msg("一首音乐",via_bot={"is_bot":True,"username":"music_demo_bot"}),{})["level"],"clean")
    def test_text_mention_bot(self):
        m=msg("点击领取",entities=[{"type":"text_mention","user":{"is_bot":True,"username":"ad_demo_bot"}}])
        self.assertEqual(classify(m,{})["reason"],"bot_promotion")
    def test_listed_bot_suspected_only(self):
        self.assertEqual(classify(msg("@ad_demo_bot"),{"blocked_bot_usernames":["ad_demo_bot"]})["level"],"suspected")
    def test_bot_boundary_not_host_spoof(self):
        self.assertEqual(classify(msg("免费领取 https://t.me.attacker.test/ad_demo_bot"),{})["level"],"clean")
    def test_seven_review_samples(self):
        for sample in SAMPLES:
            with self.subTest(sample=sample):
                self.assertEqual(classify(msg(sample), {})["level"], "suspected")
    def test_new_sales_not_in_samples(self):
        for sample in ("出售VCC信用卡，现货批发联系我", "Claude代充现货优惠下单", "服务器促销招代理", "虚拟卡免kyc，买退不卡钱"):
            with self.subTest(sample=sample):
                self.assertEqual(classify(msg(sample), {})["reason"], "category_and_sales")
    def test_ham_discussions_and_warnings(self):
        for sample in ("虚拟卡怎么用？", "Claude支付失败怎么办", "VPS内存又高了", "我用谷歌云和PayPal", "谨防VCC卡台买退骗局", "专用虚拟卡支持谷歌云是真的吗？", "GPT Plus免拒付，Claude直接过是广告样本", "这不是广告，VCC卡台先到先得", "VCC100%能过广告0开卡是真的吗？"):
            with self.subTest(sample=sample):
                self.assertEqual(classify(msg(sample), {})["level"], "clean")
    def test_behavior_needs_marketing(self):
        for field in ("forward_origin", "external_reply", "contact", "location"):
            self.assertEqual(classify(msg("分享资讯", **{field:{"id":1}}), {})["level"], "clean")
            self.assertEqual(classify(msg("现货批发先到先得", **{field:{"id":1}}), {})["reason"], "behavior_and_promotion")
    def test_similarity_reports_not_probability(self):
        message = msg(SAMPLES[2])
        verdict = classify(message,{})
        self.assertEqual(verdict["reason"], "sample_similarity")
        self.assertIn("不是广告概率",explain(verdict,message))
    def test_warning_cannot_bypass_explicit_domain(self):
        self.assertEqual(classify(msg("谨防骗局 https://example.com"),{"blocked_domains":["example.com"]})["level"], "confirmed")
    def test_large_input_bounded(self):
        self.assertEqual(classify(msg("a"*100000),{})["level"],"clean")
    def test_normal_sharing(self):
        self.assertEqual(classify(msg("分享 https://example.com 私聊讨论"), {})["level"], "clean")
    def test_suspected_not_confirmed(self):
        self.assertEqual(classify(msg("兼 职 日 结 加我 @abcdef"), {})["level"], "suspected")
    def test_blocked_subdomain(self):
        self.assertEqual(classify(msg("https://a.example.com/path"), {"blocked_domains": ["example.com"]})["level"], "confirmed")
    def test_domain_boundary(self):
        self.assertEqual(classify(msg("https://notexample.com"), {"blocked_domains": ["example.com"]})["level"], "clean")
    def test_hidden_link(self):
        self.assertEqual(classify(msg("正常文字", entities=[{"type":"text_link", "url":"https://example.com"}]), {"blocked_domains":["example.com"]})["level"], "confirmed")
    def test_zero_width(self):
        self.assertEqual(classify(msg("https://exa\u200bmple.com"), {"blocked_domains":["example.com"]})["level"], "confirmed")
    def test_caption(self):
        self.assertEqual(classify({"caption":"https://example.com"}, {"blocked_domains":["example.com"]})["level"], "confirmed")
    def test_utf16_entity(self):
        m=msg("😀 https://example.com", entities=[{"type":"url","offset":3,"length":19}])
        self.assertIn("https://example.com", extract_links(m))
    def test_invalid_domain(self):
        self.assertIsNone(domain("not a domain"))
    def test_original_card_advertisement(self):
        sample = "VCc--100%能过~AI卡段专用 廣吿0开卡，返"
        self.assertEqual(classify(msg(sample), {})["reason"], "card_promotion")
    def test_spaced_and_fullwidth_card_ad(self):
        self.assertEqual(classify(msg("Ｖ Ｃ Ｃ １００％能过 廣 吿 零 開 卡"), {})["level"], "suspected")
    def test_zero_width_card_ad(self):
        self.assertEqual(classify(msg("v\u200bc\u200bc 100%能过 广告 0开卡"), {})["level"], "suspected")
    def test_card_question_is_clean(self):
        self.assertEqual(classify(msg("VCC开卡怎么申请？AI订阅能用吗？"), {})["level"], "clean")
    def test_card_single_marketing_signal_insufficient(self):
        self.assertEqual(classify(msg("VCC免费开卡是真的吗？"), {})["level"], "clean")
    def test_card_warning_is_clean(self):
        self.assertEqual(classify(msg("谨防VCC广告，100%能过0开卡是骗局"), {})["level"], "clean")
    def test_unrelated_open_card_is_clean(self):
        self.assertEqual(classify(msg("银行今天可以免费开卡，100%能过审核"), {})["level"], "clean")
    def test_traditional_promotion(self):
        self.assertEqual(classify(msg("兼職日結 聯繫我 @abcdef"), {})["level"], "suspected")
    def test_mention_contact_preserved(self):
        self.assertEqual(classify(msg("刷单 @abcdef"), {})["level"], "suspected")
    def test_clean_explanation_not_guarantee(self):
        self.assertIn("不代表确认没有广告", explain(classify(msg("hello"), {}), msg("hello")))
    def test_media_scope_explained(self):
        image = msg("", photo=[{"file_id":"fake"}])
        self.assertIn("不识别图片", explain(classify(image, {}), image))

class BotTests(unittest.TestCase):
    def manual_api(self, fail_method=None, target_admin=False):
        original=self.api.call
        def call(method,**params):
            if method=="getChatMember":
                self.api.calls.append((method,params))
                admin=params["user_id"]==99 or target_admin
                return {"status":"administrator" if admin else "member","can_delete_messages":True,"can_restrict_members":True}
            if method==fail_method:
                self.api.calls.append((method,params));raise APIError(403)
            return original(method,**params)
        self.api.call=call
    def test_manual_success_then_learn_and_no_duplicate_ban(self):
        self.manual_api()
        command=msg("/adkill CONFIRM",uid=1,reply_to_message=msg("陌生广告文案甲乙丙丁"))
        self.bot.handle({"message":command});self.bot.handle({"message":command})
        self.assertEqual(self.methods().count("banChatMember"),1)
        self.assertEqual(self.store.policy(CID)["learned_samples"],["陌生广告文案甲乙丙丁"])
    def test_manual_delete_failure_no_ban_or_learning(self):
        self.manual_api("deleteMessage")
        self.bot.handle({"message":msg("/adkill CONFIRM",uid=1,reply_to_message=msg("陌生广告文案甲乙丙丁"))})
        self.assertNotIn("banChatMember",self.methods())
        self.assertNotIn("learned_samples",self.store.policy(CID))
    def test_manual_ban_failure_no_learning(self):
        self.manual_api("banChatMember")
        self.bot.handle({"message":msg("/adkill CONFIRM",uid=1,reply_to_message=msg("陌生广告文案甲乙丙丁"))})
        self.assertIn("deleteMessage",self.methods())
        self.assertNotIn("learned_samples",self.store.policy(CID))
    def test_manual_protects_admin(self):
        self.manual_api(target_admin=True)
        self.bot.handle({"message":msg("/adkill CONFIRM",uid=1,reply_to_message=msg("广告"))})
        self.assertNotIn("deleteMessage",self.methods())
    def test_manual_requires_confirmation(self):
        self.bot.handle({"message":msg("/adkill",uid=1,reply_to_message=msg("广告"))})
        self.assertNotIn("deleteMessage",self.methods())
    def test_manual_rejects_wrong_chat(self):
        sample=msg("广告");sample["chat"]["id"]=-999
        self.bot.handle({"message":msg("/adkill CONFIRM",uid=1,reply_to_message=sample)})
        self.assertNotIn("deleteMessage",self.methods())
    def test_nonowner_cannot_learn_or_kill(self):
        self.api.status="administrator"
        for command in ("/adlearn","/adkill CONFIRM"):
            self.bot.handle({"message":msg(command,reply_to_message=msg("陌生广告文案"))})
        self.assertNotIn("learned_samples",self.store.policy(CID))
        self.assertNotIn("deleteMessage",self.methods())
    def test_learn_forget_and_group_isolation(self):
        sample=msg("陌生广告文案甲乙丙丁")
        self.bot.handle({"message":msg("/adlearn",uid=1,reply_to_message=sample)})
        self.assertEqual(classify(sample,self.store.policy(CID))["reason"],"learned_sample")
        self.assertEqual(classify(sample,{})["level"],"clean")
        self.assertNotIn("deleteMessage",self.methods())
        self.bot.handle({"message":msg("/adforget",uid=1,reply_to_message=sample)})
        self.assertEqual(self.store.policy(CID)["learned_samples"],[])
    def test_learning_bounded_dedup_and_no_image_claim(self):
        policy=self.store.policy(CID)
        for i in range(105):self.bot.learn(CID,policy,msg("陌生广告样本序号"+str(i)))
        self.assertEqual(len(policy["learned_samples"]),100)
        self.bot.learn(CID,policy,msg("陌生广告样本序号104"))
        self.assertEqual(len(policy["learned_samples"]),100)
        self.assertIn("不能学习",self.bot.learn(CID,policy,msg("",photo=[{}])))
    def test_learned_fuzzy_suspected_not_probability(self):
        sample="独家神秘商品今日限量火热抢购立即联系店主"
        result=classify(msg(sample+"哦"),{"learned_samples":[sample]})
        self.assertEqual(result["reason"],"learned_sample")
        self.assertEqual(result["level"],"suspected")
    def private_message(self,text="",**kwargs):
        m=msg(text,uid=1,**kwargs);m["chat"]={"id":1,"type":"private"};return m
    def sent_text(self):
        return next(p["text"] for method,p in self.api.calls if method=="sendMessage")
    def test_private_start_with_payload_has_keyboard(self):
        self.bot.handle({"message":self.private_message("/start payload")})
        p=next(p for method,p in self.api.calls if method=="sendMessage")
        self.assertIn("私聊控制入口",p["text"])
        self.assertTrue(p["reply_markup"]["resize_keyboard"])
    def test_private_status_responds(self):
        self.bot.handle({"message":self.private_message("/adstatus")})
        self.assertIn("observe",self.sent_text())
    def test_private_plain_text_checked(self):
        self.bot.handle({"message":self.private_message("@ad_demo_bot 免费领取福利")})
        self.assertIn("疑似广告",self.sent_text())
        self.assertEqual(self.methods(),["sendMessage"])
    def test_private_reply_check_uses_group_domain_list_no_actions(self):
        self.mode("ban")
        self.bot.handle({"message":self.private_message("/adcheck",reply_to_message=msg("https://example.com"))})
        self.assertIn("明确规则",self.sent_text())
        self.assertEqual(self.methods(),["sendMessage"])
    def test_private_check_inline_text(self):
        self.bot.handle({"message":self.private_message("/adcheck 出售VCC现货")})
        self.assertIn("疑似广告",self.sent_text())
    def test_private_check_without_sample_has_instructions(self):
        self.bot.handle({"message":self.private_message("/adcheck")})
        self.assertIn("待检测文字",self.sent_text())
    def test_private_mutations_not_routed_to_group(self):
        self.bot.handle({"message":self.private_message("/admode ban CONFIRM")})
        self.assertIn("目标授权群",self.sent_text())
        self.assertEqual(self.store.policy(CID)["mode"],"observe")
        self.assertEqual(self.methods(),["sendMessage"])
    def test_private_unknown_command_not_silent(self):
        self.bot.handle({"message":self.private_message("/unknown")})
        self.assertIn("私聊支持",self.sent_text())
    def test_private_other_users_silent(self):
        m=self.private_message("/start");m["from"]["id"]=222
        self.bot.handle({"message":m});self.assertEqual(self.methods(),[])
    def test_private_photo_does_not_claim_vision(self):
        self.bot.handle({"message":self.private_message("",photo=[{"file_id":"fake"}])})
        self.assertIn("不识别图片画面",self.sent_text())
    def test_private_edit_not_replied(self):
        self.bot.handle({"edited_message":self.private_message("/start")})
        self.assertEqual(self.methods(),[])
    def test_menus_scoped_to_owner_and_authorized_group_admins(self):
        menus=command_menus({"owner_id":1,"groups":{str(CID):{}}})
        self.assertEqual([s["type"] for s,c in menus],["chat","chat_administrators","chat_member"])
        self.assertNotIn("admode",[c["command"] for c in menus[0][1]])
        self.assertIn("admode",[c["command"] for c in menus[2][1]])
    def test_owner_can_manage_bot_list(self):
        self.bot.handle({"message":msg("/adbot @AD_DEMO_bot",uid=1)})
        self.assertEqual(self.store.policy(CID)["blocked_bot_usernames"],["ad_demo_bot"])
        self.bot.handle({"message":msg("/adunbot @ad_demo_bot",uid=1)})
        self.assertEqual(self.store.policy(CID)["blocked_bot_usernames"],[])
    def test_nonowner_cannot_change_bot_list(self):
        self.api.status="administrator"
        self.bot.handle({"message":msg("/adbot @ad_demo_bot")})
        self.assertNotIn("blocked_bot_usernames",self.store.policy(CID))
    def test_invalid_bot_name_rejected(self):
        self.bot.handle({"message":msg("/adbot https://t.me/ad_demo_bot",uid=1)})
        self.assertNotIn("blocked_bot_usernames",self.store.policy(CID))
    def test_bot_promotion_never_auto_punished(self):
        self.mode("ban")
        self.bot.handle({"message":msg("@ad_demo_bot 免费领取福利")})
        self.assertNotIn("deleteMessage",self.methods())
        self.assertNotIn("banChatMember",self.methods())
    def test_all_new_samples_never_punished(self):
        self.mode("ban")
        for sample in SAMPLES:
            self.bot.handle({"message":msg(sample)})
        self.assertNotIn("deleteMessage",self.methods())
        self.assertNotIn("banChatMember",self.methods())
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/"db", {"groups": {str(CID): {"mode":"observe", "blocked_domains":["example.com"]}}})
        self.api = FakeAPI()
        self.bot = Bot(self.api, self.store, {"id": 99, "username":"test_bot"}, 1)
    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()
    def methods(self):
        return [c[0] for c in self.api.calls]
    def mode(self, value):
        self.store.set("group:"+str(CID), {"mode":value,"blocked_domains":["example.com"]})
    def test_observe_no_actions(self):
        self.bot.handle({"message":msg("https://example.com")})
        self.assertNotIn("deleteMessage",self.methods())
        self.assertNotIn("banChatMember",self.methods())
    def test_confirmed_delete(self):
        self.mode("delete")
        self.bot.handle({"message":msg("https://example.com")})
        self.assertIn("deleteMessage",self.methods())
        self.assertNotIn("banChatMember",self.methods())
    def test_confirmed_ban(self):
        self.mode("ban")
        self.bot.handle({"message":msg("https://example.com")})
        self.assertIn("banChatMember",self.methods())
    def test_suspected_never_banned(self):
        self.mode("ban")
        self.bot.handle({"message":msg("稳赚 加我 @abcdef")})
        self.assertNotIn("deleteMessage",self.methods())
    def test_admin_exempt(self):
        self.mode("ban");self.api.status="administrator"
        self.bot.handle({"message":msg("https://example.com")})
        self.assertNotIn("banChatMember",self.methods())
    def test_owner_exempt(self):
        self.mode("ban")
        self.bot.handle({"message":msg("https://example.com",uid=1)})
        self.assertEqual(self.methods(),[])
    def test_channel_identity_not_banned(self):
        self.mode("ban")
        self.bot.handle({"message":msg("https://example.com",sender_chat={"id":-12})})
        self.assertNotIn("banChatMember",self.methods())
    def test_unknown_group_ignored(self):
        m=msg("https://example.com");m["chat"]["id"]=-888
        self.bot.handle({"message":m})
        self.assertEqual(self.methods(),[])
    def test_role_failure_no_action(self):
        self.mode("ban");self.api.fail=True
        with self.assertRaises(APIError):self.bot.handle({"message":msg("https://example.com")})
        self.assertNotIn("deleteMessage",self.methods())
    def test_edit_scanned(self):
        self.mode("delete")
        self.bot.handle({"edited_message":msg("https://example.com")})
        self.assertIn("deleteMessage",self.methods())
    def test_nonowner_cannot_change_mode(self):
        self.api.status="administrator"
        self.bot.handle({"message":msg("/admode delete")})
        self.assertEqual(self.store.policy(CID)["mode"],"observe")
    def test_ban_needs_confirmation(self):
        self.api.status="administrator"
        self.bot.handle({"message":msg("/admode ban",uid=1)})
        self.assertEqual(self.store.policy(CID)["mode"],"observe")
    def test_manual_check_no_action(self):
        self.api.status="administrator";self.mode("ban")
        self.bot.handle({"message":msg("/adcheck",reply_to_message=msg("https://example.com"))})
        self.assertNotIn("deleteMessage",self.methods())
    def test_no_message_bodies_saved(self):
        self.bot.handle({"message":msg("https://example.com secret-text")})
        row=self.store.db.execute("SELECT * FROM events").fetchone()
        self.assertNotIn("secret-text",repr(row))
    def test_token_not_in_exception(self):
        with patch("bot.Telegram._open",side_effect=ValueError("SECRET")):
            with self.assertRaises(APIError) as caught:Telegram("SECRET").call("getMe")
        self.assertNotIn("SECRET",str(caught.exception))
    def test_policy_change_has_recovery_snapshot(self):
        self.mode("delete")
        self.assertEqual(len(list(Path(self.temp.name).glob("policy-before-*.db"))),1)
    def test_snapshot_retention_is_bounded(self):
        for _ in range(9):self.mode("observe")
        self.assertEqual(len(list(Path(self.temp.name).glob("policy-before-*.db"))),5)
    def test_command_prefix_cannot_bypass_detection(self):
        self.mode("delete")
        self.bot.handle({"message":msg("/adstatus https://example.com")})
        self.assertIn("deleteMessage",self.methods())
    def test_card_ad_never_automatically_punished(self):
        self.mode("ban")
        self.bot.handle({"message":msg("VCC 100%能过 广告0开卡")})
        self.assertNotIn("deleteMessage",self.methods())
        self.assertNotIn("banChatMember",self.methods())
    def test_check_returns_chinese(self):
        self.api.status="administrator"
        self.bot.handle({"message":msg("/adcheck",reply_to_message=msg("VCC 100%能过 廣吿0开卡"))})
        text = next(params["text"] for method,params in self.api.calls if method=="sendMessage")
        self.assertIn("疑似广告",text)
        self.assertIn("不删除、不封禁",text)

class OutboxTests(unittest.TestCase):
    def test_blocked_sender_does_not_block_enqueue(self):
        api=Telegram("FAKE")
        entered=threading.Event();release=threading.Event();second=threading.Event()
        def call(method,**params):
            if params["text"]=="one":
                entered.set();release.wait(2)
            else:
                second.set()
            return True
        api.call=call
        api.start_outbox()
        try:
            api.enqueue_send(chat_id=CID,text="one")
            self.assertTrue(entered.wait(1))
            api.enqueue_send(chat_id=CID,text="two")
            self.assertTrue(second.wait(1))
        finally:
            release.set();api.drain_outbox(3)
        self.assertEqual(api.outbox.unfinished_tasks,0)
    def test_send_failure_is_recorded_and_queue_finishes(self):
        api=Telegram("FAKE")
        api.call=lambda *a,**k: (_ for _ in ()).throw(APIError(403))
        with self.assertLogs("ad-killer",level="WARNING") as logs:
            api.start_outbox();api.enqueue_send(chat_id=CID,text="test");api.drain_outbox(2)
        self.assertIn("reply_failed",str(logs.output))
        self.assertNotIn("reply_sent",str(logs.output))
    def test_queue_overload_bounded(self):
        import queue
        api=Telegram("FAKE");api.outbox=queue.Queue(maxsize=1)
        api.enqueue_send(chat_id=CID,text="one")
        with self.assertLogs("ad-killer",level="ERROR"):
            with self.assertRaises(APIError):api.enqueue_send(chat_id=CID,text="two")

if __name__ == "__main__":
    unittest.main()

class CodeEntityTests(unittest.TestCase):
    def test_identifiers_after_emoji_have_utf16_offsets(self):
        from bot import code_entities
        text='😀 案件 AD-000006\n用户ID：100000001\n用户名：@demo_user\n/adkill CONFIRM'
        spans=code_entities(text);raw=text.encode('utf-16-le')
        values=[raw[e['offset']*2:(e['offset']+e['length'])*2].decode('utf-16-le') for e in spans]
        self.assertEqual(values,['AD-000006','100000001','@demo_user','/adkill CONFIRM'])
    def test_arbitrary_html_not_interpreted(self):
        from bot import code_entities
        self.assertEqual(code_entities('<b>普通内容 & 😀</b>'),[])
