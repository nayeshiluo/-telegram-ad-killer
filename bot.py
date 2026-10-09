#!/usr/bin/env python3
"""Independent Telegram moderation pilot. Stdlib only; fail closed on uncertainty."""
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import queue
import re
import threading
import sqlite3
import time
import urllib.error
import urllib.request

from rules import classify, domain, explain, fingerprint
from policy import Repeats, features, filtered, verdict as policy_verdict
from management import COMMANDS as MANAGEMENT_COMMANDS, MENU as MANAGEMENT_MENU

LOG = logging.getLogger("ad-killer")
ADMIN = {"creator", "administrator"}
HELP = (
    "广告杀手 v1.0.1\n默认观察，不自动删除、不自动封人。\n"
    "/adstatus 查看状态\n/adcheck 回复消息检测（群管理员）\n"
    "规则与模式设置仅主人可用：\n/adblock 域名 添加黑名单\n/adunblock 域名 移除黑名单\n"
    "/adbot @用户名 添加广告机器人疑似名单\n/adunbot @用户名 移除名单\n"
    "/admode observe 观察\n/admode delete 删除明确黑名单消息\n"
    "/admode ban CONFIRM 删除并封禁；Telegram 可能清除被封者历史消息\n"
    "群管理可用：\n/adkill CONFIRM 回复广告，删除封禁，成功后学习\n/adlearn 回复广告，只学习\n/adforget 回复原文，撤回学习\n"
    "/adreview 回复消息创建复核；观察模式只模拟\n/adcase 编号 查看案件\n/admode review CONFIRM 开启三分钟复核处罚（仅主人）\n"
    "/admanage 打开名单与案件管理面板\n/adblacklist 本群有效封禁名单\n/adhistory 用户ID 查案件\n/adunban 案件编号 解封保留样本\n/adwrong 案件编号 纠正误封\n/adwhite add/remove 用户ID 本群白名单\n/adgwhite add/remove 用户ID 全局白名单（仅主人）\n/adwhitelist 查看白名单\n/adaudit 查看变更记录\n"
    "/adsettings 打开本群检测开关\n被封成员可私聊 /adappeal AD-编号 申诉说明\n观察模式不自动处罚。AI不确定不启动倒计时。"
)
PRIVATE_COMMANDS = [
    {"command": "start", "description": "查看使用说明"},
    {"command": "help", "description": "查看功能与使用方法"},
    {"command": "adstatus", "description": "查看授权群状态"},
    {"command": "adcheck", "description": "回复消息检测广告"},
]
OWNER_GROUP_COMMANDS = PRIVATE_COMMANDS + [
    {"command": "adblock", "description": "添加黑名单域名，需要参数"},
    {"command": "adunblock", "description": "移除黑名单域名，需要参数"},
    {"command": "adbot", "description": "添加广告机器人疑似名单"},
    {"command": "adunbot", "description": "移除广告机器人疑似名单"},
    {"command": "admode", "description": "设置观察或处罚模式，需要参数"},
    {"command": "adkill", "description": "回复广告删除封禁；必须加 CONFIRM"},
    {"command": "adlearn", "description": "回复广告学习文本，不处罚"},
    {"command": "adforget", "description": "回复原文撤回学习，不解封"},
]
CASE_COMMANDS=[{"command":"adreview","description":"回复消息创建复核；观察模式仅测试"},
               {"command":"adcase","description":"查询本群案件，需编号"}]
ADMIN_GROUP_COMMANDS=PRIVATE_COMMANDS+CASE_COMMANDS+[{"command":"adkill","description":"回复广告主动删除封禁；需 CONFIRM"}]
OWNER_GROUP_COMMANDS += CASE_COMMANDS + MANAGEMENT_MENU + [{"command":"adgwhite","description":"全局白名单 add/remove 用户ID；仅主人"}]
ADMIN_GROUP_COMMANDS += MANAGEMENT_MENU
OWNER_PRIVATE_COMMANDS=PRIVATE_COMMANDS+[{"command":"admanage","description":"打开授权群的名单和案件管理面板"},{"command":"adsettings","description":"打开授权群检测开关"},{"command":"adgwhite","description":"全局白名单 add/remove 用户ID"}]

def command_menus(config):
    menus = [({"type": "chat", "chat_id": config["owner_id"]}, OWNER_PRIVATE_COMMANDS)]
    for cid in config["groups"]:
        menus.append(({"type": "chat_administrators", "chat_id": int(cid)}, ADMIN_GROUP_COMMANDS))
        menus.append(({"type": "chat_member", "chat_id": int(cid), "user_id": config["owner_id"]}, OWNER_GROUP_COMMANDS))
    return menus


def code_entities(text):
    """Telegram code spans use UTF-16 offsets; never parse user text as markup."""
    pattern=r'(?:用户ID|群ID|原消息ID|证据消息|归档频道|ID)[：:]\s*(?P<numeric>-?\d+)|(?<![A-Za-z0-9_])AD-\d+|(?<![A-Za-z0-9_])@[A-Za-z0-9_]{5,32}|(?<![A-Za-z0-9_])/ad[a-z]+(?: CONFIRM)?|(?<![\w])-100\d{5,}'
    spans=[]
    for match in re.finditer(pattern,text):
        start=match.start('numeric') if match.group('numeric') is not None else match.start()
        value=match.group('numeric') if match.group('numeric') is not None else match.group()
        spans.append({'type':'code','offset':len(text[:start].encode('utf-16-le'))//2,'length':len(value.encode('utf-16-le'))//2})
    return spans


class APIError(Exception):
    def __init__(self, code=0, retry_after=0, kind="api"):
        self.code, self.retry_after, self.kind = code, retry_after, kind
        super().__init__("telegram_error_" + str(code))


class Telegram:
    def __init__(self, token):
        self.token = token
        self.outbox = None
        from transport import opener
        self.opener=opener()

    def _open(self,request,timeout):
        return self.opener.open(request,timeout=timeout)

    def start_outbox(self):
        self.outbox = queue.Queue(maxsize=100)
        self.workers = []
        for _ in range(2):
            worker = threading.Thread(target=self._sender, daemon=True)
            worker.start()
            self.workers.append(worker)

    def enqueue_send(self, **params):
        if self.outbox is None:
            return self.call("sendMessage", **params)
        try:
            self.outbox.put_nowait((time.monotonic(), params))
        except queue.Full:
            LOG.error("reply_queue_full chat=%s", params.get("chat_id"))
            raise APIError()

    def _sender(self):
        while True:
            queued, params = self.outbox.get()
            try:
                self.call("sendMessage", **params)
                LOG.info("reply_sent chat=%s message=%s queue_and_send_ms=%s", params.get("chat_id"),
                         params.get("reply_parameters", {}).get("message_id"), round((time.monotonic()-queued)*1000))
            except APIError as exc:
                LOG.warning("reply_failed chat=%s code=%s", params.get("chat_id"), exc.code)
                if exc.code == 429:
                    time.sleep(max(1, min(exc.retry_after, 30)))
            except Exception as exc:
                LOG.error("reply_failed type=%s", type(exc).__name__)
            finally:
                self.outbox.task_done()

    def drain_outbox(self, timeout=12):
        if self.outbox is None:
            return
        until = time.monotonic() + timeout
        while self.outbox.unfinished_tasks and time.monotonic() < until:
            time.sleep(0.05)
        if self.outbox.unfinished_tasks:
            LOG.warning("shutdown_pending_replies count=%s", self.outbox.unfinished_tasks)

    def call(self, method, **params):
        if method in {"sendMessage", "editMessageText"} and params.get("text") and "parse_mode" not in params and "entities" not in params:
            spans=code_entities(params["text"])
            match=re.search(r"\n原文摘录：(.*?)\n状态：",params["text"],re.S)
            if match:
                start=len(params["text"][:match.start(1)].encode("utf-16-le"))//2
                length=len(match[1].encode("utf-16-le"))//2
                spans=[v for v in spans if v["offset"]+v["length"]<=start or v["offset"]>=start+length]
                if length:spans.append({"type":"spoiler","offset":start,"length":length})
            if spans:params["entities"]=spans
        started=time.monotonic()
        request = urllib.request.Request(
            "https://api.telegram.org/bot" + self.token + "/" + method,
            data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with self._open(request, timeout=min(35, max(8, int(params.get("timeout", 0))+7)) if method == "getUpdates" else 8) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            retry = 0
            kind = "api"
            try:
                error = json.load(exc)
                retry = error.get("parameters", {}).get("retry_after", 0)
                description = str(error.get("description", "")).lower()
                if "query is too old" in description or "query id is invalid" in description:
                    kind = "expired_callback"
                elif method == 'copyMessage' and 'message to copy not found' in description:
                    kind = 'message_missing'
                elif method == 'deleteMessage' and 'message to delete not found' in description:
                    kind = 'message_missing'
            except Exception:
                pass
            LOG.warning("telegram_failed method=%s code=%s kind=%s", method, exc.code, kind)
            raise APIError(exc.code, min(int(retry), 300), kind) from None
        except Exception as exc:
            # URL exceptions can contain the token: never log original exception.
            cause=getattr(exc,"reason",exc)
            safe_kind=type(cause).__name__
            raise APIError(kind=safe_kind if safe_kind.isidentifier() else "transport") from None
        finally:
            if method != "getUpdates":LOG.info("telegram_call method=%s duration_ms=%s",method,round((time.monotonic()-started)*1000))
        if not data.get("ok"):
            raise APIError(data.get("error_code", 0))
        return data["result"]


class Store:
    def __init__(self, path, config):
        self.path = Path(path)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=3000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY, ts INTEGER, chat_id INTEGER,
                message_id INTEGER, user_id INTEGER, level TEXT, reason TEXT, action TEXT);
        """)
        for cid, policy in config["groups"].items():
            self.db.execute("INSERT OR IGNORE INTO kv VALUES (?,?)", ("group:" + cid, json.dumps(policy)))
        self.db.commit()
        self.config = config

    def get(self, key, fallback=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else fallback

    def set(self, key, value):
        if key.startswith("group:"):
            # Keep recoverable policy snapshots; never alter credentials.
            stamp = str(time.time_ns())
            backup = sqlite3.connect(self.path.parent / ("policy-before-" + stamp + ".db"))
            try:
                self.db.backup(backup)
            finally:
                backup.close()
            snapshots = sorted(self.path.parent.glob("policy-before-*.db"))
            for old in snapshots[:-5]:
                old.unlink()
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))
        self.db.commit()

    def policy(self, cid):
        if str(cid) not in self.config["groups"]:
            return None
        return self.get("group:" + str(cid))

    def event(self, message, verdict, action):
        self.db.execute("INSERT INTO events(ts,chat_id,message_id,user_id,level,reason,action) VALUES (?,?,?,?,?,?,?)", (
            int(time.time()), message["chat"]["id"], message["message_id"],
            message.get("from", {}).get("id"), verdict["level"], verdict["reason"], action))
        # Bounded on-disk metadata only; no message bodies, media, or credentials.
        self.db.execute("DELETE FROM events WHERE id < (SELECT COALESCE(MAX(id),0)-5000 FROM events)")
        self.db.commit()


class Bot:
    def __init__(self, api, store, identity, owner_id, ai=None):
        self.api, self.store, self.identity, self.owner = api, store, identity, owner_id
        self.ai = ai
        self.cases = None
        self.management = None
        self.repeats = Repeats()
        if self.store.config.get("archive_channel"):
            from cases import Cases
            self.cases=Cases(self,self.store.config["archive_channel"])
            from management import Management
            self.management=Management(self)

    def ai_status(self):
        return "AI文字/照片检测已接入；是否处罚取决于本群模式与案件证据，不按模型单独结论封禁。\n"+self.ai.status() if self.ai else "AI和图片画面识别尚未接入。"

    def ai_check(self, sample, reply):
        if not self.ai:return
        # Inline /adcheck text may lack a Telegram message identity.
        sample={**sample,"chat":sample.get("chat",reply["chat"]),"message_id":sample.get("message_id",reply["message_id"])}
        if not self.ai.submit(sample, reply):self.send(reply,"AI队列已满或无可检测内容；未完成AI检测。")

    def send(self, message, text, reply_markup=None):
        sender = getattr(self.api, "enqueue_send", None)
        if sender is None:
            sender = lambda **params: self.api.call("sendMessage", **params)
        extra = {"reply_markup": reply_markup} if reply_markup is not None else {}
        sender(chat_id=message["chat"]["id"], text=text,
                      reply_parameters={"message_id": message["message_id"]},
                      link_preview_options={"is_disabled": True}, **extra)

    def private(self, message):
        if message.get("from", {}).get("id") != self.owner:
            if self.cases:self.cases.appeal(message)
            return
        text = (message.get("text") or "").strip()
        raw = text.split(maxsplit=1)
        name = raw[0].split("@")[0].casefold() if raw else ""
        if self.management and name in MANAGEMENT_COMMANDS:
            self.management.command(message,(message.get("text") or "").split());return
        if name in {"/start", "/help"}:
            self.send(message, "私聊控制入口\n发送文字即可检测；也可回复消息发送 /adcheck。\n"
                      "模式和名单修改请在目标群执行。\n" + self.ai_status()+"\n\n" + HELP,
                      reply_markup={"keyboard": [[{"text": "/adstatus"}, {"text": "/help"}], [{"text": "/adcheck"}]],
                                    "resize_keyboard": True, "is_persistent": True})
            return
        if name == "/adstatus":
            lines = ["广告杀手在线", "授权群数：" + str(len(self.store.config["groups"]))]
            for cid in self.store.config["groups"]:
                policy = self.store.policy(int(cid))
                lines.append("群 " + cid + "\n模式：" + policy["mode"] +
                             "；域名名单：" + str(len(policy["blocked_domains"])) +
                             "；机器人疑似名单：" + str(len(policy.get("blocked_bot_usernames", []))))
            lines.append("观察模式不自动处罚；review会按案件流程处罚。"+self.ai_status())
            if self.cases:lines.append(self.cases.status())
            self.send(message, "\n".join(lines))
            return
        if name in {"/admode", "/adblock", "/adunblock", "/adbot", "/adunbot", "/adkill", "/adlearn", "/adforget", "/adreview", "/adcase"}:
            self.send(message, "请在目标授权群执行此指令。私聊不修改群模式或名单。")
            return
        if name.startswith("/") and name != "/adcheck":
            self.send(message, "私聊支持 /start、/help、/adstatus、/adcheck，也可直接发送文字检测。")
            return
        sample = message
        if name == "/adcheck":
            sample = message.get("reply_to_message")
            if sample is None and len(raw) > 1:
                sample = {"text": raw[1]}
            if sample is None:
                self.send(message, "请回复消息发送 /adcheck，或发送 /adcheck 待检测文字。")
                return
        elif not text and not message.get("caption"):
            if self.ai and message.get("photo"):
                self.ai_check(message,message)
                return
            self.send(message, "请回复这条消息发送 /adcheck。当前只检测附带文字和链接，不识别图片画面。")
            return
        groups = list(self.store.config["groups"])
        policy = self.store.policy(int(groups[0])) if len(groups) == 1 else {}
        result = classify(sample, policy)
        response = explain(result, sample)
        if len(groups) != 1:
            response += "\n多群私聊只使用基础规则；群名单请在目标群回复 /adcheck 检测。"
        self.send(message, response)
        self.ai_check(sample, message)

    def admin(self, cid, uid):
        return self.api.call("getChatMember", chat_id=cid, user_id=uid).get("status") in ADMIN

    def learn(self, cid, policy, sample, forget=False):
        text = (sample.get("text") or sample.get("caption") or "")[:512]
        if not text and self.ai:
            from cases import message_key
            row=self.store.db.execute("SELECT observed_text FROM ai_reviews WHERE chat_id=? AND message_id=? AND label='spam' AND digest=?",
                 (cid,sample.get("message_id"),message_key(sample))).fetchone()
            text=row[0] if row else ""
        key = fingerprint(text)
        if len(key) < 3:
            return "没有可学习的文字；图片画面尚不能学习。"
        samples = list(policy.get("learned_samples", []))
        existing = [s for s in samples if fingerprint(s) == key]
        if forget:
            samples = [s for s in samples if fingerprint(s) != key]
            status = "已撤回学习（不恢复消息、不解除封禁）。" if existing else "原文不在学习库中。"
        elif existing:
            return "文本已在学习库中，没有重复添加。"
        else:
            samples = (samples + [text])[-100:]
            status = "已学习，立即生效；相似文案只标记疑似，不自动封禁。"
        policy["learned_samples"] = samples
        self.store.set("group:" + str(cid), policy)
        return status + "本群学习样本数：" + str(len(samples))

    def manual_kill(self, message, policy, raw):
        sample = message.get("reply_to_message")
        if raw[1:] != ["CONFIRM"] or not sample:
            self.send(message, "回复广告发送 /adkill CONFIRM：删除并永久封禁，成功后学习文字。Telegram可能清除该用户历史消息，无法恢复。仅你可用。")
            return
        cid = message["chat"]["id"]
        if sample.get("chat", {}).get("id") != cid or not isinstance(sample.get("message_id"), int) or sample["message_id"] <= 0:
            self.send(message, "回复目标不属于本群或消息编号无效，未执行。")
            return
        uid = sample.get("from", {}).get("id")
        if sample.get("sender_chat") or not uid or uid in {self.owner, self.identity["id"]}:
            self.send(message, "不能处罚主人、机器人自身或频道/匿名身份；学习文字可用 /adlearn。")
            return
        if self.admin(cid, uid):
            self.send(message, "目标是群主或管理员，不执行处罚。")
            return
        me = self.api.call("getChatMember", chat_id=cid, user_id=self.identity["id"])
        if me.get("status") != "administrator" or not me.get("can_delete_messages") or not me.get("can_restrict_members"):
            self.send(message, "删除或封禁权限不足，未执行、未学习。")
            return
        key = "manual:" + str(cid) + ":" + str(sample["message_id"])
        previous = self.store.get(key)
        if previous:
            self.send(message, "此消息已提交处理，状态：" + previous + "。不重复处罚；不确定结果请人工核查。")
            return
        self.store.set(key, "requested")
        rows = self.store.db.execute("SELECT key FROM kv WHERE key LIKE 'manual:%' ORDER BY rowid DESC LIMIT -1 OFFSET 500").fetchall()
        self.store.db.executemany("DELETE FROM kv WHERE key=?", rows)
        self.store.db.commit()
        verdict = {"level": "confirmed", "reason": "owner_manual"}
        stage = "requested"
        try:
            self.api.call("deleteMessage", chat_id=cid, message_id=sample["message_id"])
            stage = "deleted";self.store.set(key, stage)
            self.api.call("banChatMember", chat_id=cid, user_id=uid)
            stage = "banned";self.store.set(key, stage)
        except APIError as exc:
            self.store.event(sample, verdict, stage + "_failed_" + str(exc.code))
            self.send(message, "处理未全部成功，阶段：" + stage + "，错误码：" + str(exc.code) + "。未学习、不自动重试；超时结果可能不确定，请人工核查。")
            return
        self.store.event(sample, verdict, "manually_banned")
        try:
            learned = self.learn(cid, policy, sample)
        except Exception:
            self.send(message, "删除和封禁成功，但学习保存失败。")
            return
        self.send(message, "删除、封禁成功。" + learned)

    def command(self, message, policy):
        raw = (message.get("text") or "").split()
        if not raw or not raw[0].startswith("/"):
            return False
        name, _, target = raw[0].partition("@")
        if target and target.casefold() != self.identity["username"].casefold():
            return False
        if name not in ({"/start", "/help", "/adstatus", "/adcheck", "/admode", "/adblock", "/adunblock", "/adbot", "/adunbot", "/adkill", "/adlearn", "/adforget", "/adreview", "/adcase"} | MANAGEMENT_COMMANDS):
            return False
        uid = message.get("from", {}).get("id")
        if message.get("sender_chat") or not uid:
            return False
        cid = message["chat"]["id"]
        if uid != self.owner and not self.admin(cid, uid):
            # A known command prefix must not let ordinary users bypass scanning.
            return False
        if name in MANAGEMENT_COMMANDS:
            if self.management:self.management.command(message,raw)
            else:self.send(message,"案件管理系统尚未配置。")
        elif name in {"/start", "/help"}:
            self.send(message, HELP)
        elif name == "/adstatus":
            self.send(message, "广告杀手在线\n模式：" + policy["mode"] +
                      "\n黑名单域名数：" + str(len(policy["blocked_domains"])) +
                      "\n广告机器人疑似名单数：" + str(len(policy.get("blocked_bot_usernames", []))) +
                      "\n本群学习样本数：" + str(len(policy.get("learned_samples", []))) +
                      "\n观察模式不自动处罚；review按案件流程处罚；管理员豁免。")
            self.send(message,self.ai_status())
            if self.cases:self.send(message,self.cases.status(cid))
        elif name == "/adcheck":
            replied = message.get("reply_to_message")
            if not replied:
                self.send(message, "请回复一条消息发送 /adcheck。")
            else:
                result = classify(replied, policy)
                self.send(message, explain(result, replied)+( "\n以上仅规则检测，AI结果另行回复。" if self.ai else ""))
                self.ai_check(replied,message)
        elif name in {"/adreview", "/adcase"} and not self.cases:
            self.send(message, "案件归档系统未配置，此命令不可用。")
        elif self.cases and name == "/adreview":
            sample=message.get("reply_to_message")
            if not sample or sample.get("chat",{}).get("id")!=cid:
                self.send(message,"请回复本群消息创建复核。")
            else:
                self.cases.track(sample)
                c=self.cases.open(sample,"管理员发起复核",eligible=policy["mode"] in {"observe","review"},dry=policy["mode"]!="review")
                if not c:self.send(message,"目标受保护或待复核案件已满，未创建。")
        elif self.cases and name == "/adcase":
            try:number=int(raw[1].removeprefix("AD-"))
            except (ValueError,IndexError):number=0
            c=self.cases.get(number)
            self.send(message,self.cases.text(c) if c and c["cid"]==cid else "没有这个本群案件；用法 /adcase AD-000001。")
        elif self.cases and name == "/adkill":
            if raw[1:]!=["CONFIRM"]:
                self.send(message,"回复广告发送 /adkill CONFIRM；会永久封禁且可能清除历史消息，无法恢复。")
            else:self.cases.manual(message)
        elif uid != self.owner:
            self.send(message, "仅机器人主人可以修改规则或处罚模式。")
        elif name == "/adkill":
            self.manual_kill(message, policy, raw)
        elif name in {"/adlearn", "/adforget"}:
            sample = message.get("reply_to_message")
            self.send(message, self.learn(cid, policy, sample, forget=name == "/adforget") if sample else "请回复原文执行；只学习或撤回文字，不处罚。")
        elif name == "/admode":
            mode = raw[1] if len(raw) > 1 else ""
            if mode not in {"observe", "delete", "ban", "review"} or (mode in {"ban","review"} and raw[2:] != ["CONFIRM"]):
                self.send(message, "用法：/admode observe、delete；ban 或 review 必须加 CONFIRM。")
            else:
                if mode=="review":
                    if not self.cases:
                        self.send(message,"案件归档系统未配置，不能开启 review。");return True
                    try:
                        self.cases.ready(cid)
                    except (RuntimeError, APIError):
                        self.send(message,"群或归档频道权限不足，或权限查询失败；模式未变更。")
                        return True
                me = self.api.call("getChatMember", chat_id=cid, user_id=self.identity["id"])
                if mode != "observe" and (me.get("status") != "administrator" or not me.get("can_delete_messages") or (mode == "ban" and not me.get("can_restrict_members"))):
                    self.send(message, "权限不足，模式未变更。")
                else:
                    policy["mode"] = mode
                    self.store.set("group:" + str(cid), policy)
                    self.send(message, "已设置模式：" + mode)
        elif name in {"/adbot", "/adunbot"}:
            username = raw[1].lstrip("@").casefold() if len(raw) == 2 else ""
            if not re.fullmatch(r"[a-z][a-z0-9_]{4,31}", username) or not username.endswith("bot"):
                self.send(message, "用法：/adbot @机器人用户名 或 /adunbot @机器人用户名。")
            else:
                blocked = set(policy.get("blocked_bot_usernames", []))
                if name == "/adbot":
                    if len(blocked) >= 500 and username not in blocked:
                        self.send(message, "疑似机器人名单已达500条上限。")
                        return True
                    blocked.add(username)
                else:
                    blocked.discard(username)
                policy["blocked_bot_usernames"] = sorted(blocked)
                self.store.set("group:" + str(cid), policy)
                self.send(message, "已更新广告机器人疑似名单；只标记，不凭此自动处罚。")
        elif name in {"/adblock", "/adunblock"}:
            host = domain(raw[1]) if len(raw) == 2 else None
            if not host:
                self.send(message, "请输入一个有效域名，不要输入关键词。")
            else:
                blocked = set(policy["blocked_domains"])
                if name == "/adblock":
                    if len(blocked) >= 500 and host not in blocked:
                        self.send(message, "黑名单已达500条上限。")
                        return True
                    blocked.add(host)
                else:
                    blocked.discard(host)
                policy["blocked_domains"] = sorted(blocked)
                self.store.set("group:" + str(cid), policy)
                self.send(message, "已更新黑名单。")
        return True

    def handle(self, update):
        if update.get("callback_query") and self.cases:
            q=update["callback_query"]
            if self.management and str(q.get("data","")).startswith("adm:"):self.management.callback(q)
            else:self.cases.callback(q)
            return
        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        cid = message.get("chat", {}).get("id")
        uid = message.get("from", {}).get("id")
        if message.get("chat", {}).get("type") == "private":
            if "message" in update:
                self.private(message)
            return
        policy = self.store.policy(cid)
        if policy is None:
            return
        if self.cases:self.cases.track(message)
        # Commands may change policy only on new messages, never on edits.
        if "message" in update and self.command(message, policy):
            return
        if uid == self.owner:
            return
        if self.management and self.management.whitelisted(cid,uid):return
        switches=features(policy)
        verdict = policy_verdict(message, policy)
        if self.cases and self.cases.consider(message,verdict):return
        # Suspicious rule hits check admins before repeat counting; the AI worker checks other messages.
        if message.get("sender_chat") or not uid:
            if verdict["level"]!="clean":self.store.event(message,verdict,"sender_chat_review_only")
            return
        if verdict['level']!='clean' and self.admin(cid, uid):return
        repeated=self.repeats.check(message,policy,verdict)
        if repeated:
            verdict=repeated
            if self.cases and policy["mode"]=="review":
                self.cases.open(message,"短时间重复推广；仍需人工复核",eligible=False,dry=False)
        if self.ai and switches["ai"]:
            if not self.ai.submit({**message,"_ad_features":switches},urgent=verdict['level']!='clean') and self.cases:
                self.cases.ai_fallback(message,"AI队列满或消息不可检测")
        if verdict["level"] == "clean":
            return
        # Channel/anonymous messages never get mapped to a fake user for bans.
        if message.get("sender_chat") or not uid:
            self.store.event(message, verdict, "sender_chat_review_only")
            return
        action = "observed"
        if verdict["level"] == "confirmed" and policy["mode"] in {"delete", "ban"}:
            if self.cases:
                c=self.cases.open(message,"明确黑名单规则",dry=False,announce=False)
                if c:self.cases.execute(c["id"],"explicit_domain",ban=policy["mode"]=="ban")
                return
            self.store.event(message, verdict, "action_pending")
            try:
                self.api.call("deleteMessage", chat_id=cid, message_id=message["message_id"])
                action = "deleted"
                if policy["mode"] == "ban":
                    self.api.call("banChatMember", chat_id=cid, user_id=uid)
                    action = "banned"
            except APIError as exc:
                self.store.event(message, verdict, action + "_action_failed_" + str(exc.code))
                raise
        self.store.event(message, verdict, action)
        LOG.info("classification chat=%s message=%s level=%s action=%s", cid, message["message_id"], verdict["level"], action)


def main():
    state = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/ad-killer"))
    logs = Path(os.environ.get("LOGS_DIRECTORY", "/var/log/ad-killer"))
    state.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    LOG.setLevel(logging.INFO)
    handler = RotatingFileHandler(logs / "bot.log", maxBytes=1024*1024, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    config = json.loads(Path(os.environ.get("AD_CONFIG", "/etc/ad-killer/config.json")).read_text())
    credential_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    token_path = Path(credential_dir) / "bot-token" if credential_dir else Path("/etc/ad-killer/bot-token")
    api = Telegram(token_path.read_text().strip())
    identity = api.call("getMe")
    if identity.get("username", "").casefold() != config["expected_username"].casefold():
        raise RuntimeError("unexpected_bot_identity")
    if api.call("getWebhookInfo").get("url"):
        raise RuntimeError("webhook_exists_not_modified")
    store = Store(state / "state.db", config)
    api.start_outbox()
    ai = None
    if config.get("ai",{}).get("enabled"):
        from ai import AIClient, ReviewWorker
        key_path=Path(credential_dir)/"ai-key" if credential_dir else Path("/etc/ad-killer/ai-key")
        ai=ReviewWorker(AIClient(config["ai"],key_path.read_text().strip()),api,store.path,config["owner_id"])
    bot = Bot(api, store, identity, config["owner_id"], ai=ai)
    for scope,commands in command_menus(config):
        try:api.call("setMyCommands",scope=scope,commands=commands)
        except APIError as exc:LOG.warning("menu_registration_failed code=%s",exc.code)
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    offset = store.get("offset", 0)
    LOG.info("startup username=%s authorized_groups=%s", identity["username"], len(config["groups"]))
    while not stopping:
        try:
            if bot.cases:
                if ai:
                    for _ in range(20):
                        try:sample,result=ai.results.get_nowait()
                        except queue.Empty:break
                        try:bot.cases.ai_result(sample,result)
                        except Exception as exc:LOG.warning('ai_case_failed type=%s',type(exc).__name__)
                        finally:ai.results.task_done()
            updates = api.call("getUpdates", offset=offset, timeout=1 if bot.cases else 25, limit=100,
                               allowed_updates=["message", "edited_message", "my_chat_member", "callback_query"])
            for update in updates:
                started = time.monotonic()
                try:
                    bot.handle(update)
                except APIError as exc:
                    # Do not retry destructive actions on the same update.
                    LOG.warning("update_api_failed update=%s code=%s", update["update_id"], exc.code)
                except Exception as exc:
                    LOG.error("update_failed update=%s type=%s", update["update_id"], type(exc).__name__)
                offset = update["update_id"] + 1
                store.set("offset", offset)
                message = update.get("message") or update.get("edited_message") or {}
                lag = max(0, int(time.time())-int(message["date"])) if message.get("date") else -1
                kind = "callback" if update.get("callback_query") else "message"
                LOG.info("update_processed update=%s processing_ms=%s receive_age_s=%s kind=%s", update["update_id"], round((time.monotonic()-started)*1000), lag, kind)
            if bot.cases:bot.cases.tick()
            store.set("heartbeat", int(time.time()))
        except APIError as exc:
            LOG.warning("poll_failed code=%s kind=%s", exc.code,exc.kind)
            if exc.code in {401, 409}:
                raise SystemExit(1)
            time.sleep(max(3, min(exc.retry_after, 300)))
    if ai:ai.close()
    api.drain_outbox()
    store.db.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        LOG.error("fatal type=%s", type(exc).__name__)
        raise SystemExit(1) from None
