"""Bounded, deterministic matching; no network, AI, or message history."""
import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import urlsplit

URL = re.compile(r"(?:https?://|www\.)[^\s<>]+|(?:t\.me|telegram\.me)/[^\s<>]+", re.I)
PROMOTION = ("包赔", "稳赚", "日赚", "高额返佣", "博彩", "网赌", "刷单", "兼职日结", "代开发票")
CONTACT = re.compile(r"私聊|加我|联系|进群|点击|领取|代理|返佣|点我|点头像|客服|加v|(?<![a-z0-9_])vx(?![a-z0-9_])|薇信|v信|@[a-z0-9_]{5,}", re.I)
AD_VARIANTS = str.maketrans({
    "廣": "广", "吿": "告", "開": "开", "專": "专", "傭": "佣",
    "聯": "联", "繫": "系", "絡": "络", "領": "领", "穩": "稳",
    "賺": "赚", "額": "额", "職": "职", "結": "结", "發": "发", "騙": "骗",
    "虛": "虚", "擬": "拟", "訂": "订", "穏": "稳", "咔": "卡", "臺": "台",
    "賭": "赌", "號": "号", "貸": "贷", "換": "换", "匯": "汇", "幣": "币",
    "盤": "盘", "價": "价", "銷": "销", "獎": "奖", "無": "无", "費": "费",
})
CARD = re.compile(r"(?<![a-z])v[\W_]*c[\W_]*c(?![a-z])|虚拟信用卡|虚拟卡", re.I)
CARD_GUARANTEE = re.compile(r"100\s*%\s*(?:能过|通过|成功)|保证(?:能过|通过|成功)|包过")
REASONS = {
    "blocked_domain": "命中你配置的黑名单域名",
    "promotion_and_contact": "推广话术与联系方式或引流话术同时出现",
    "card_promotion": "开卡产品与多项推广信号同时出现",
    "no_match": "当前规则没有命中；不代表确认没有广告",
    "category_and_sales": "业务类别与推销信号同时出现",
    "sample_similarity": "与已收集的疑似广告样本高度相似；需要复核",
    "behavior_and_promotion": "消息行为与推广信号同时出现",
    "bot_promotion": "机器人入口与推广、引流信号同时出现",
    "listed_bot": "命中主人设置的广告机器人名单；需要复核",
    "learned_sample": "与主人在本群确认的广告样本一致或高度相似",
    "repeated_promotion": "本群短时间内重复出现相同推广内容；需要复核",
    "paid_boost_promotion": "代刷或刷量业务同时包含收益承诺、推销或引流信号",
}
# Selected terms informed by AzurLab/Tg-Ad-RegEx, not its broad delete rules.
BUSINESS = re.compile(r"vcc|虚拟(?:信用)?卡|卡台|卡段|💳|博彩|网赌|棋牌|赌场|亚博|哈希投注|刷单|兼职日结|跑分|代开发票|代收|代付|代充|代购|账号|微信号|支付宝号|成品号|白号|接码|服务器|vps|cdn|高防|独服|机场|引流|增粉|吸粉|加粉|粉丝|云服务|谷歌云|paypal|gpt|claude|信用卡|银行卡|四件套|卡商|usdt|换汇|套现|网贷|贷款|高仿|莆田|企业签名|苹果签名|社工库|查档|查人|数据出售")
SALES = re.compile(r"出售|售卖|批发|现货|供应|货源|下单|购买|价格|优惠|促销|招代理|先到先得|量有限|免kyc|买退|不卡钱|免拒付|直接过|稳定跑|速刷|超刷|投流|顶级政策|专用|包过|担保|返佣|免费试用|免费测试")
WARNING = re.compile(r"不要信|别信|谨防|骗局|避坑|举报|反诈|被骗|诈骗|不是广告|广告样本")
QUESTION = re.compile(r"怎么|如何|能用吗|可以用吗|是真的吗|怎么办|请问|求助|为什么|失败|不支持")
BOT_PUSH = re.compile(r"点击|点我|领取|进群|返佣|免费|优惠|下单|购买|抽奖|红包|福利|资源|推广|查档|搜索")

def bot_references(message):
    """Candidate names from mentions, links, and real inline bot metadata.

    A *bot suffix is a routing signal, not evidence that an account is malicious.
    Never visit links or resolve arbitrary usernames over the network.
    """
    text = normalize(message.get("text") or message.get("caption") or "")
    names = set(re.findall(r"(?<![a-z0-9_])@([a-z][a-z0-9_]{4,31})(?![a-z0-9_])", text))
    bots = {name for name in names if name.endswith("bot")}
    for link in extract_links(message):
        try:
            parsed = urlsplit(link if "://" in link else "https://" + link)
            if parsed.hostname and parsed.hostname.casefold() in {"t.me", "telegram.me"}:
                name = parsed.path.strip("/").split("/")[0].casefold()
                if re.fullmatch(r"[a-z][a-z0-9_]{4,31}", name) and name.endswith("bot"):
                    bots.add(name)
        except ValueError:
            pass
    via = message.get("via_bot") or {}
    if via.get("is_bot"):
        bots.add(str(via.get("username") or "inline_bot").casefold())
    for ent in (message.get("entities") or message.get("caption_entities") or [])[:128]:
        user = ent.get("user") or {}
        if ent.get("type") == "text_mention" and user.get("is_bot"):
            bots.add(str(user.get("username") or "mentioned_bot").casefold())
    return sorted(bots)[:64]
# Curated review samples supplied during acceptance testing, not collected chat history.
SAMPLES = (
    "專用虚拟咔：云服务/AI訂/TK/FB投放...",
    "VCC卡台支持全模型AI退款撤销100%支持，量有限先到先得！",
    "虚拟卡：谷歌云-等云服务-PayPal-支持",
    "虚拟卡-Claude20-100-200-免kyc=买退不卡钱",
    "虚拟卡AI速刷超刷5🔪--投流广告顶级政策0",
    "GPT Plus免拒付，Claude直接过，Fb TK穏定跑",
    "💳台：做买退CC/GPT，訂AI-100%撤/退",
)
# Short public README example excerpt, curated as suspected (not ground-truth data).
# https://github.com/MarkIvory2973/tg-spam , retrieved 2026-10-06.
PUBLIC_REVIEW_SAMPLES = ("缺几个敢拼的兄弟😊，跟我干通宵，下个月路虎开回家",)

def fingerprint(text):
    return re.sub(r"[\W_]+", "", normalize(text).translate(AD_VARIANTS))[:512]

LEVELS = {"confirmed": "命中明确规则", "suspected": "疑似广告", "clean": "未命中当前规则"}


def explain(result, message):
    text = "检测：" + LEVELS[result["level"]] + "\n原因：" + REASONS[result["reason"]]
    if result.get("signals"):
        text += "\n依据：" + "、".join(result["signals"])
    if result.get("similarity") is not None:
        text += "\n样本相似度：" + str(round(result["similarity"] * 100)) + "%（不是广告概率）"
    if message.get("photo") or message.get("video") or message.get("document"):
        text += "\n范围：只检查消息文字、说明和链接，不识别图片或视频里的文字。"
    return text + "\n本次只检测，不删除、不封禁。"


def normalize(text):
    text = unicodedata.normalize("NFKC", text[:16384]).casefold()
    return "".join(c for c in text if unicodedata.category(c) != "Cf")

SAMPLE_KEYS = tuple(fingerprint(s) for s in SAMPLES + PUBLIC_REVIEW_SAMPLES)


def domain(value):
    value = value.strip().casefold()
    try:
        host = urlsplit(value if "://" in value else "https://" + value).hostname
        if not host or "." not in host or any(c.isspace() for c in host):
            return None
        return host.encode("idna").decode("ascii").rstrip(".")
    except (ValueError, UnicodeError):
        return None


def on_domain(host, root):
    return host == root or host.endswith("." + root)


def extract_links(message):
    text = message.get("text") or message.get("caption") or ""
    links = URL.findall(normalize(text))
    encoded = text.encode("utf-16-le")
    for ent in (message.get("entities") or message.get("caption_entities") or []):
        if ent.get("type") == "text_link" and ent.get("url"):
            links.append(ent["url"])
        elif ent.get("type") == "url":
            start = ent.get("offset", 0) * 2
            end = start + ent.get("length", 0) * 2
            links.append(encoded[start:end].decode("utf-16-le", errors="replace"))
    return links[:128]


def classify(message, policy):
    text = normalize(message.get("text") or message.get("caption") or "")
    hosts = {domain(normalize(url)) for url in extract_links(message)} - {None}
    blocked = policy.get("blocked_domains", [])
    hits = sorted(h for h in hosts if any(on_domain(h, d) for d in blocked))
    if hits:
        return {"level": "confirmed", "reason": "blocked_domain", "domains": hits}
    ad_text = text.translate(AD_VARIANTS)
    compact = re.sub(r"[\W_]+", "", ad_text)
    contact_text = re.sub(r"\s+", "", ad_text)
    # Weak warning/question messages are not auto labelled; explicit blocked domains
    # are checked first and cannot be bypassed by adding a question or warning.
    # A bot deep link's ?start= is not a human question mark.
    visible = URL.sub("", ad_text)
    visible_compact = re.sub(r"[\W_]+", "", visible)
    caution = bool(WARNING.search(visible_compact) or QUESTION.search(visible_compact) or re.search(r"[?？]", visible))
    key = fingerprint(ad_text)
    learned = [fingerprint(s) for s in policy.get("learned_samples", [])[:100] if isinstance(s, str)]
    if len(key) >= 3 and key in learned:
        return {"level": "suspected", "reason": "learned_sample", "domains": [], "similarity": 1.0}
    if not caution and 12 <= len(key) <= 512:
        similarity = max((SequenceMatcher(None,key,s,autojunk=False).ratio() for s in learned
                          if abs(len(key)-len(s)) <= max(len(key),len(s))*0.2),default=0)
        if similarity >= 0.9:
            return {"level": "suspected", "reason": "learned_sample", "domains": [], "similarity": similarity}
    bots = bot_references(message)
    listed = set(policy.get("blocked_bot_usernames", []))
    if bots and not caution and not policy.get('disable_bot_rules'):
        if any(name in listed for name in bots):
            return {"level": "suspected", "reason": "listed_bot", "domains": [], "signals": bots[:4]}
        if BOT_PUSH.search(compact):
            return {"level": "suspected", "reason": "bot_promotion", "domains": [],
                    "signals": ["机器人入口", *bots[:3], "推广或引流"]}
    if CARD.search(ad_text) and "开卡" in compact:
        signals = sum((
            "广告" in compact,
            bool(re.search(r"(?:0|零|免费)开卡", compact)),
            bool(CARD_GUARANTEE.search(ad_text)),
            bool(CONTACT.search(contact_text)),
        ))
        warning = any(word in compact for word in ("不要信", "别信", "谨防", "骗局", "避坑", "举报"))
        if signals >= 2 and not warning and not caution:
            return {"level": "suspected", "reason": "card_promotion", "domains": []}
    if any(term in compact for term in PROMOTION) and CONTACT.search(contact_text):
        if not caution:
            return {"level": "suspected", "reason": "promotion_and_contact", "domains": []}
    if not caution:
        if re.search(r'代刷|刷量|刷粉|刷赞|刷播放',compact) and (
                re.search(r'(?:一天|日赚|日入|月入)\d+(?:千|万|k|w|元)',compact)
                or SALES.search(compact) or CONTACT.search(contact_text)):
            return {'level':'suspected','reason':'paid_boost_promotion','domains':[],
                    'signals':['代刷/刷量业务','收益承诺或推销引流']}
        business = BUSINESS.findall(compact)
        if "💳" in ad_text:
            business.append("卡产品")
        sales = SALES.findall(compact)
        contact = bool(CONTACT.search(contact_text))
        # Category words, mentions and price discussions alone are weak evidence.
        offer=bool(re.search(r"出售|售卖|批发|现货|供应|货源|下单|招代理|促销|返佣|免费试用|免费测试|免kyc|买退|不卡钱|免拒付|直接过|稳定跑|速刷|超刷|投流|顶级政策|包过|担保",compact))
        solicitation=bool(re.search(r"私聊|加我|联系客服|进群|点击|领取|点我|点头像|加v|(?<![a-z0-9_])vx(?![a-z0-9_])|薇信|v信",contact_text))
        if business and (offer or solicitation or (re.search(r"优惠|购买|先到先得|量有限",compact) and contact)):
            return {"level": "suspected", "reason": "category_and_sales", "domains": [],
                    "signals": list(dict.fromkeys(business + sales + (["联系或引流"] if contact else [])))[:8]}
        # Structure alone is insufficient: a normal forward/location is not spam.
        behavior = bool(message.get("external_reply") or message.get("forward_origin") or
                        message.get("contact") or message.get("location"))
        if behavior and sales:
            return {"level": "suspected", "reason": "behavior_and_promotion", "domains": [],
                    "signals": ["外部引用、转发、名片或定位", *sales[:4]]}
        key = fingerprint(ad_text)
        # Length and input caps prevent unbounded quadratic similarity work.
        if 12 <= len(key) <= 512:
            similarity = max((SequenceMatcher(None, key, s, autojunk=False).ratio()
                              for s in SAMPLE_KEYS if abs(len(key) - len(s)) <= max(len(key),len(s))*0.25), default=0)
            if similarity >= 0.88:
                return {"level": "suspected", "reason": "sample_similarity", "domains": [], "similarity": similarity}
    return {"level": "clean", "reason": "no_match", "domains": []}
