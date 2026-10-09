"""Persistent moderation cases. Telegram side effects are staged, never blind-retried."""
import hashlib
import json
import logging
import re
import time
from rules import classify, fingerprint
from policy import verdict as policy_verdict, features

LOG=logging.getLogger('ad-killer')
ADMIN={'creator','administrator'}
STATES={'pending':'待复核','held':'离线积压，需人工复核','preparing':'创建中','review_post_failed':'复核消息发送失败',
        'archiving':'归档中','archiving_failed':'归档失败，未处罚','delete_pending':'正在删帖','delete_pending_failed':'删帖失败或结果不确定，未继续封禁',
        'ban_pending':'正在封禁','ban_pending_failed':'删帖完成，封禁失败或结果不确定','banned':'已删除并封禁','deleted':'已删除，未封禁',
        'simulated_ban':'模拟封禁完成，未实际处罚','protected':'目标受权限保护，已停止','member_rejected':'群友三票驳回','admin_rejected':'管理驳回',
        'edited':'原文已编辑，旧案件取消','mode_cancelled':'模式已变更，自动处罚取消','unban_pending':'正在解除封禁',
        'unban_failed':'解除失败或结果不确定','unbanned':'已解除本群封禁','wrong_unbanned':'已纠正误封并撤回样本',
        'cancelled_unban':'已解封，相关待处理案件取消','superseded':'存在较新处罚，请操作最新记录','pending_failed':'权限核查失败，未处罚',
        'deleted_protected':'删帖后目标身份变化，已停止封禁','whitelisted':'已加入白名单，复核取消'}
def state_label(value):return STATES.get(value,'处理异常，需核对：'+value)

def public_reason(value):
    """Internal moderator identifiers must never appear in public case reasons."""
    return re.sub(r"admin:\d+", "管理员确认", value)

def message_key(message):
    fields={k:message.get(k) for k in ('text','caption','entities','caption_entities','photo','video','document','via_bot','forward_origin','external_reply','contact','location')}
    return hashlib.sha256(json.dumps(fields,sort_keys=True).encode()).hexdigest()

class Cases:
    def __init__(self,bot,channel):
        self.bot,self.api,self.store,self.channel=bot,bot.api,bot.store,channel
        self.db=self.store.db
        self.db.row_factory=__import__('sqlite3').Row
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS cases(id INTEGER PRIMARY KEY AUTOINCREMENT,cid INTEGER,mid INTEGER,uid INTEGER,
            created INTEGER,deadline INTEGER,state TEXT,reason TEXT,payload TEXT,report_id INTEGER DEFAULT 0,
            archive_id INTEGER DEFAULT 0,dry INTEGER DEFAULT 1,learn_text TEXT DEFAULT '',archive_text TEXT DEFAULT '',revision INTEGER DEFAULT 0,UNIQUE(cid,mid,revision));
          CREATE TABLE IF NOT EXISTS case_votes(case_id INTEGER,voter INTEGER,PRIMARY KEY(case_id,voter));
          CREATE TABLE IF NOT EXISTS active_bans(cid INTEGER,uid INTEGER,case_id INTEGER,active INTEGER,PRIMARY KEY(cid,uid));
          CREATE TABLE IF NOT EXISTS case_samples(cid INTEGER,key TEXT,case_id INTEGER,text TEXT,active INTEGER,
            PRIMARY KEY(cid,key,case_id));
          CREATE TABLE IF NOT EXISTS case_seen(cid INTEGER,mid INTEGER,digest TEXT,PRIMARY KEY(cid,mid));
          CREATE INDEX IF NOT EXISTS case_pending ON cases(state,deadline);
          CREATE TABLE IF NOT EXISTS appeals(case_id INTEGER PRIMARY KEY,uid INTEGER,created INTEGER,text TEXT);
          CREATE TABLE IF NOT EXISTS case_notice_cleanup(case_id INTEGER PRIMARY KEY,cid INTEGER,mid INTEGER,due INTEGER,attempts INTEGER DEFAULT 0,state TEXT DEFAULT 'pending');
        ''')
        if 'revision' not in {row[1] for row in self.db.execute('PRAGMA table_info(cases)')}:
            # Preserve IDs referenced by votes, samples, bans and appeals.
            # Atomic migration: old data remains usable if the transaction fails.
            backup=__import__('sqlite3').connect(self.store.path.parent/'schema-before-revisions.db')
            try:self.db.backup(backup)
            finally:backup.close()
            with self.db:
                self.db.execute("CREATE TABLE cases_v2(id INTEGER PRIMARY KEY AUTOINCREMENT,cid INTEGER,mid INTEGER,uid INTEGER,created INTEGER,deadline INTEGER,state TEXT,reason TEXT,payload TEXT,report_id INTEGER DEFAULT 0,archive_id INTEGER DEFAULT 0,dry INTEGER DEFAULT 1,learn_text TEXT DEFAULT '',archive_text TEXT DEFAULT '',revision INTEGER DEFAULT 0,UNIQUE(cid,mid,revision))")
                self.db.execute('INSERT INTO cases_v2 SELECT *,0 FROM cases')
                self.db.execute('DROP TABLE cases')
                self.db.execute('ALTER TABLE cases_v2 RENAME TO cases')
                self.db.execute('CREATE INDEX case_pending ON cases(state,deadline)')
        # Only run at process initialization: interrupted destructive calls are
        # uncertain, not safe to replay. Keep them reachable for manual recovery.
        interrupted={'preparing':'review_post_failed','archiving':'archiving_failed',
                     'delete_pending':'delete_pending_failed','ban_pending':'ban_pending_failed',
                     'unban_pending':'unban_failed'}
        for old,new in interrupted.items():
            changed=self.db.execute('UPDATE cases SET state=? WHERE state=?',(new,old)).rowcount
            if changed:LOG.warning('case_restart_recovery from=%s to=%s count=%s',old,new,changed)
        self.db.commit()

    def get(self,n):
        row=self.db.execute('SELECT * FROM cases WHERE id=?',(n,)).fetchone()
        return dict(row) if row else None

    def set_state(self,n,state):
        self.db.execute('UPDATE cases SET state=? WHERE id=?',(state,n));self.db.commit()

    def track(self,message):
        cid=message['chat']['id'];mid=message['message_id'];digest=message_key(message)
        previous=self.db.execute('SELECT digest FROM case_seen WHERE cid=? AND mid=?',(cid,mid)).fetchone()
        if previous and previous[0]!=digest:
            rows=self.db.execute("SELECT id FROM cases WHERE cid=? AND mid=? AND state IN ('pending','held')",(cid,mid)).fetchall()
            for row in rows:self.set_state(row[0],'edited');self.refresh(row[0])
        self.db.execute('INSERT OR REPLACE INTO case_seen VALUES(?,?,?)',(cid,mid,digest))
        self.db.execute('DELETE FROM case_seen WHERE rowid NOT IN (SELECT rowid FROM case_seen ORDER BY rowid DESC LIMIT 5000)')
        self.db.commit()

    def protected(self,message):
        uid=message.get('from',{}).get('id');cid=message['chat']['id']
        if message.get('sender_chat') or not uid or uid in {self.bot.owner,self.bot.identity['id']}:return True
        if self.bot.management and self.bot.management.whitelisted(cid,uid):return True
        role=self.api.call('getChatMember',chat_id=cid,user_id=uid).get('status')
        if role not in {'member','restricted'}:return True
        return False

    def ready(self,cid,ban=True):
        role=self.api.call('getChatMember',chat_id=cid,user_id=self.bot.identity['id'])
        if role.get('status')!='administrator' or not role.get('can_delete_messages') or (ban and not role.get('can_restrict_members')):
            raise RuntimeError('source_permissions_missing')
        role=self.api.call('getChatMember',chat_id=self.channel,user_id=self.bot.identity['id'])
        if role.get('status')!='administrator' or not role.get('can_post_messages'):
            raise RuntimeError('archive_permissions_missing')

    def snapshot(self,m):
        out={k:m[k] for k in ('chat','from','message_id','photo','video','document','entities','caption_entities','via_bot','forward_origin','external_reply','contact','location') if k in m}
        out['text']=(m.get('text') or '')[:4096];out['caption']=(m.get('caption') or '')[:1024]
        return out

    def number(self,n):return 'AD-'+str(n).zfill(6)

    def keyboard(self,c):
        if c['state'] not in {'pending','held'}:return {'inline_keyboard':[]}
        n=c['id'];votes=self.db.execute('SELECT COUNT(*) FROM case_votes WHERE case_id=?',(n,)).fetchone()[0]
        return {'inline_keyboard':[
            [{'text':'群友驳回 '+str(votes)+'/3','callback_data':'adcase:vote:'+str(n)}],
            [{'text':'管理驳回','callback_data':'adcase:reject:'+str(n)},
             {'text':'核实封禁','callback_data':'adcase:ban:'+str(n)}]]}

    def text(self,c):
        m=json.loads(c['payload']);who=m.get('from',{})
        deadline=('截止：'+time.strftime('%H:%M:%S',time.gmtime(c['deadline']+8*3600))+'（北京时间）') if c['deadline'] else '仅管理员复核，无超时处罚'
        chat=m.get('chat',{})
        appeal=self.db.execute('SELECT text FROM appeals WHERE case_id=?',(c['id'],)).fetchone()
        return (self.number(c['id'])+' '+('观察测试，绝不实际处罚' if c['dry'] else '广告复核')+
                '\n来源群：'+str(chat.get('title') or c['cid'])+'\n群ID：'+str(c['cid'])+
                '\n显示名：'+str(who.get('first_name',''))+' '+str(who.get('last_name',''))+
                '\n用户ID：'+str(c['uid'])+'\n用户名：'+('@'+who['username'] if who.get('username') else '无')+
                '\n时间（北京时间）：'+time.strftime('%Y-%m-%d %H:%M:%S',time.gmtime(c['created']+28800))+
                '\n判定来源：'+('AI复核' if c['reason'].startswith('AI：') else '规则或管理员')+
                '\n理由：'+public_reason(c['reason'])[:300]+'\n'+deadline+'\n状态：'+state_label(c['state'])+
                '\n需3名不同群成员驳回，或1名本群管理决策。'+
                ('\n本人申诉：'+appeal[0] if appeal else ''))

    def refresh(self,n):
        c=self.get(n)
        if not c or not c['report_id']:return
        try:self.api.call('editMessageText',chat_id=c['cid'],message_id=c['report_id'],text=self.text(c),reply_markup=self.keyboard(c),link_preview_options={'is_disabled':True})
        except Exception:LOG.warning('case_ui_refresh_failed case=%s',n)

    def open(self,message,reason,eligible=False,dry=None,announce=True):
        cid=message['chat']['id'];mid=message['message_id'];policy=self.store.policy(cid)
        if policy is None:return None
        previous=self.db.execute('SELECT id,revision,state FROM cases WHERE cid=? AND mid=? ORDER BY revision DESC LIMIT 1',(cid,mid)).fetchone()
        if previous and previous['state']!='edited':return self.get(previous['id'])
        revision=previous['revision']+1 if previous else 0
        overflow=announce and self.db.execute("SELECT COUNT(*) FROM cases WHERE cid=? AND report_id>0 AND state IN ('pending','held')",(cid,)).fetchone()[0]>=20
        if overflow and self.db.execute("SELECT COUNT(*) FROM cases WHERE cid=? AND report_id=0 AND state='held'",(cid,)).fetchone()[0]>=200:
            self.store.event(message,{'level':'suspected','reason':'review_capacity'},'review_overflow_full')
            LOG.error('case_overflow_full chat=%s message=%s',cid,mid);return None
        if overflow:
            announce=False;eligible=False
            reason='复核卡片满额，仅人工处理；'+reason
            LOG.warning('case_pending_limit_retained chat=%s message=%s',cid,mid)
        dry=(policy['mode']!='review') if dry is None else dry
        if not dry and self.protected(message):return None
        now=int(time.time());deadline=now+180 if eligible else 0
        row=self.db.execute('INSERT INTO cases(cid,mid,uid,created,deadline,state,reason,payload,dry,revision) VALUES(?,?,?,?,?,?,?,?,?,?)',
             (cid,mid,message.get('from',{}).get('id'),now,deadline,'preparing',reason[:300],json.dumps(self.snapshot(message)),int(dry),revision))
        self.db.commit();n=row.lastrowid
        if announce:
            try:
                c=self.get(n);c['state']='pending'
                posted=self.api.call('sendMessage',chat_id=cid,text=self.text(c),reply_parameters={'message_id':mid,'allow_sending_without_reply':True},reply_markup=self.keyboard(c),link_preview_options={'is_disabled':True})
                self.db.execute("UPDATE cases SET report_id=?,state='pending' WHERE id=?",(posted['message_id'],n));self.db.commit()
            except Exception:self.set_state(n,'review_post_failed');raise
        else:self.set_state(n,'held' if overflow else 'pending')
        return self.get(n)

    def consider(self,message,result=None):
        cid=message['chat']['id'];policy=self.store.policy(cid)
        if policy is None or policy['mode']!='review':return False
        uid=message.get("from",{}).get("id")
        if message.get("sender_chat") or not uid or uid in {self.bot.owner,self.bot.identity["id"]}:return True
        black=self.db.execute('SELECT active FROM active_bans WHERE cid=? AND uid=?',(cid,uid)).fetchone()
        text=(message.get('text') or message.get('caption') or '')
        key=fingerprint(text)
        exact=self.db.execute('SELECT 1 FROM case_samples WHERE cid=? AND key=? AND active=1',(cid,key)).fetchone() if 20<=len(key) and len(text)<=512 else None
        if (black and black[0]) or exact:
            if self.protected(message):return True
            c=self.open(message,'有效ID黑名单' if black and black[0] else '管理员已确认的独特广告原文',dry=False,announce=False)
            if c and c['state']=='pending':self.execute(c['id'],'id_repeat' if black and black[0] else 'sample_repeat')
            return True
        verdict=result or policy_verdict(message,policy)
        if verdict.get('level')=='confirmed':self.open(message,verdict['reason'],eligible=True,dry=False)
        elif verdict.get('level')=='suspected' and (not self.bot.ai or not features(policy)['ai']):
            self.open(message,'规则疑似：'+verdict['reason']+'；无AI复核，仅人工决策',eligible=False,dry=False)
        return False

    def ai_fallback(self,message,reason):
        policy=self.store.policy(message["chat"]["id"])
        if not policy or policy["mode"]!="review":return
        if "_ad_features" in message and message["_ad_features"]!=features(policy):return
        seen=self.db.execute("SELECT digest FROM case_seen WHERE cid=? AND mid=?",(message["chat"]["id"],message["message_id"])).fetchone()
        if not seen or seen[0]!=message_key(message):return
        evidence=policy_verdict(message,policy)
        if evidence["level"]!="clean":
            self.open(message,"规则疑似："+evidence["reason"]+"；"+reason+"，仅人工复核",eligible=False,dry=False)

    def ai_result(self,message,result):
        if result.get("label") in {"error","uncertain"}:
            self.ai_fallback(message,"AI失败、超时或无法确定");return
        cid=message['chat']['id'];policy=self.store.policy(cid)
        if policy is None or policy['mode']!='review' or not features(policy)['ai'] or result.get('label')!='spam':return
        if '_ad_features' in message and message['_ad_features']!=features(policy):return
        seen=self.db.execute('SELECT digest FROM case_seen WHERE cid=? AND mid=?',(cid,message['message_id'])).fetchone()
        if not seen or seen[0]!=message_key(message) or self.protected(message):return
        evidence=policy_verdict(message,policy)
        ocr=result.get('observed_text','')
        ocr_hit=features(policy)['photo'] and policy_verdict({'text':ocr},policy)['level']!='clean' if len(fingerprint(ocr))>=20 else False
        eligible=evidence['level']!='clean' or ocr_hit
        c=self.open(message,'AI：'+result.get('reason','')+('；规则/可读广告文字同时命中' if eligible else '；证据不足，不自动处罚'),eligible=eligible,dry=False)
        if c and ocr:
            self.db.execute('UPDATE cases SET learn_text=? WHERE id=?',(ocr[:512],c['id']));self.db.commit()

    def archive(self,c):
        if c['archive_id']:return
        m=json.loads(c['payload']);u=m.get('from',{});chat=m.get('chat',{})
        evidence='原消息复制'
        try:
            copied=self.api.call('copyMessage',chat_id=self.channel,from_chat_id=c['cid'],message_id=c['mid'],disable_notification=True)
        except Exception as exc:
            if getattr(exc,'kind',None)!='message_missing':raise
            evidence='原消息已不存在；使用机器人接收时的快照，无法核实删除者'
            copied=self.archive_snapshot(c,m)
        link='https://t.me/'+chat['username']+'/'+str(c['mid']) if re.fullmatch(r'[A-Za-z0-9_]+',str(chat.get('username',''))) else '私密群原消息ID：'+str(c['mid'])
        note=self.number(c['id'])+(' 观察测试记录（未实际处罚）' if c['dry'] else ' 处罚记录')+'\n来源群：'+str(chat.get('title') or c['cid'])+'\n群ID：'+str(c['cid'])+'\n用户ID：'+str(c['uid'])+'\n用户名：'+('@'+u['username'] if u.get('username') else '无')+'\n显示名：'+str(u.get('first_name',''))+' '+str(u.get('last_name',''))+'\n时间（北京时间）：'+time.strftime('%Y-%m-%d %H:%M:%S',time.gmtime(c['created']+28800))+'\n判定来源：'+('AI复核' if c['reason'].startswith('AI：') else '规则或管理员')+'\n来源：'+link+'\n理由：'+public_reason(c['reason'])+'\n证据消息：'+str(copied['message_id'])+'\n原文摘录：'+(m.get('text') or m.get('caption') or '照片/媒体，见证据消息')[:600]+'\n状态：'+(state_label(c['state']) if c['dry'] else '处罚准备中，尚未确认成功')
        note=note.replace('\n证据消息：','\n证据保存：'+evidence+'\n证据消息：')
        markup={'inline_keyboard':[[{'text':self.number(c['id'])+' 查看状态','callback_data':'adcase:info:'+str(c['id'])}]]}
        if c['dry']:markup={'inline_keyboard':[[{'text':self.number(c['id'])+' 查看状态','callback_data':'adcase:info:'+str(c['id'])}]]}
        record=self.api.call('sendMessage',chat_id=self.channel,text=note[:4000],reply_markup=markup,link_preview_options={'is_disabled':True},disable_notification=False)
        self.db.execute('UPDATE cases SET archive_id=?,archive_text=? WHERE id=?',(record['message_id'],note[:4000],c['id']));self.db.commit()

    def archive_snapshot(self,c,m):
        # Reuse Telegram media IDs; never download arbitrary URLs or fabricate an original copy.
        for key,method,arg in (('photo','sendPhoto','photo'),('video','sendVideo','video'),('document','sendDocument','document')):
            media=m.get(key)
            if not media:continue
            item=media[-1] if key=='photo' else media
            try:
                return self.api.call(method,chat_id=self.channel,**{arg:item['file_id']},caption=m.get('caption','')[:1024],disable_notification=True)
            except Exception as exc:
                if getattr(exc,'code',0)!=400:raise
                LOG.warning('archive_snapshot_media_unavailable case=%s',c['id'])
        text=m.get('text') or m.get('caption') or c['learn_text']
        if not text:raise RuntimeError('archive_snapshot_unavailable')
        if any(m.get(k) for k in ('photo','video','document')):
            text='媒体无法重发；以下为已保留的说明文字/OCR（不等于原图）：\n'+text
        return self.api.call('sendMessage',chat_id=self.channel,text=text[:4096],link_preview_options={'is_disabled':True},disable_notification=True)

    def archive_state(self,n):
        c=self.get(n)
        if not c or not c['archive_id']:return
        text=public_reason(c['archive_text']).rsplit('\n状态：',1)[0]+'\n状态：'+state_label(c['state'])+'\n更新（北京时间）：'+time.strftime('%m-%d %H:%M:%S',time.gmtime(time.time()+8*3600))
        deleted=c['state'] in {'banned','deleted','ban_pending_failed','deleted_protected','unbanned','wrong_unbanned','unban_failed'}
        text+='\n删除结果：'+('已确认删除' if deleted else '未确认删除')+'\n封禁结果：'+('已确认封禁' if c['state']=='banned' else '见案件状态；未确认仍在封禁')
        if '原消息已不存在' in c['archive_text']:
            text=text.replace('删除结果：已确认删除','删除结果：原消息已不存在，无法核实删除者')
        appeal=self.db.execute('SELECT text FROM appeals WHERE case_id=?',(n,)).fetchone()
        if appeal:text+='\n本人申诉待管理员审核：'+appeal[0]
        if c['dry']:text=self.number(n)+' 观察测试记录（未实际处罚）\n'+text.partition('\n')[2]
        markup={'inline_keyboard':[[{'text':self.number(n)+' 查看状态','callback_data':'adcase:info:'+str(n)}]]}
        if not c['dry'] and c['state'] in {'banned','ban_pending_failed','unban_failed','unbanned'}:
            markup['inline_keyboard'].append([{'text':'解除本群封禁','callback_data':'adcase:unban:'+str(n)},{'text':'判定误封并撤回学习','callback_data':'adcase:wrong:'+str(n)}])
        try:self.api.call('editMessageText',chat_id=self.channel,message_id=c['archive_id'],text=text,link_preview_options={'is_disabled':True},reply_markup=markup)
        except Exception:LOG.warning('archive_state_failed case=%s',n)

    def execute(self,n,actor,ban=True):
        c=self.get(n)
        if not c or c['state'] not in {'pending','held'}:return
        if c['dry']:
            self.set_state(n,'simulated_ban');self.refresh(n);self.archive_observation(n);return
        m=json.loads(c['payload'])
        try:
            if self.protected(m):self.set_state(n,'protected');self.refresh(n);return
            self.ready(c['cid'],ban=ban)
            c['reason']=c['reason']+'；处理来源：'+public_reason(actor)
            self.db.execute('UPDATE cases SET reason=? WHERE id=?',(public_reason(c['reason'])[:300],n));self.db.commit()
            self.set_state(n,'archiving');self.archive(c)
            if self.protected(m):
                self.set_state(n,'protected');self.refresh(n);self.archive_state(n);return
            self.set_state(n,'delete_pending')
            try:self.api.call('deleteMessage',chat_id=c['cid'],message_id=c['mid'])
            except Exception as exc:
                if getattr(exc,'kind',None)!='message_missing':raise
                self.db.execute("UPDATE cases SET archive_text=replace(archive_text,'\\n证据消息：','\\n原消息已不存在，无法核实删除者\\n证据消息：') WHERE id=?",(n,));self.db.commit()
            if not ban:
                self.set_state(n,'deleted');self.refresh(n);self.archive_state(n);return
            if self.protected(m):
                self.set_state(n,'deleted_protected');self.refresh(n);self.archive_state(n);return
            self.set_state(n,'ban_pending')
            self.api.call('banChatMember',chat_id=c['cid'],user_id=c['uid'])
            with self.db:
                self.db.execute("UPDATE cases SET state='banned' WHERE id=?",(n,))
                self.db.execute('INSERT OR REPLACE INTO active_bans VALUES(?,?,?,1)',(c['cid'],c['uid'],n))
                if c['report_id']:
                    self.db.execute("INSERT OR IGNORE INTO case_notice_cleanup(case_id,cid,mid,due) VALUES(?,?,?,?)",(n,c['cid'],c['report_id'],int(time.time())+180))
                if actor.startswith('admin:'):
                    text=(c['learn_text'] or m.get('text') or m.get('caption') or '')[:512];key=fingerprint(text)
                    if len(key)>=20:
                        self.db.execute('INSERT OR REPLACE INTO case_samples VALUES(?,?,?,?,1)',(c['cid'],key,n,text))
                        self.db.execute('DELETE FROM case_samples WHERE cid=? AND rowid NOT IN (SELECT rowid FROM case_samples WHERE cid=? ORDER BY rowid DESC LIMIT 1000)',(c['cid'],c['cid']))
            self.store.event(m,{'level':'confirmed','reason':actor},'case_banned')
        except Exception as exc:
            self.db.rollback()
            current=self.get(n)['state']
            if current!='banned':self.set_state(n,current+'_failed')
            LOG.warning('case_action_failed case=%s stage=%s type=%s',n,current,type(exc).__name__)
        self.refresh(n);self.archive_state(n)

    def manual(self,message):
        sample=message.get('reply_to_message')
        if not sample or sample.get('chat',{}).get('id')!=message['chat']['id']:
            self.bot.send(message,'请在本群回复广告发送 /adkill CONFIRM。');return
        if self.protected(sample):self.bot.send(message,'目标受到身份保护，未处罚。');return
        c=self.open(sample,'管理员主动确认',dry=False,announce=False)
        if not c:return
        # Upgrade an untouched observation case only by an explicit administrative command.
        if c['state'] in {'pending','held','member_rejected','admin_rejected','simulated_ban','edited','archiving_failed','pending_failed'}:
            # A newly confirmed body requires new evidence, even when the same
            # message was previously tested or edited. Old buttons become stale.
            self.db.execute("UPDATE cases SET dry=0,state='pending',deadline=0,reason='管理员主动确认',payload=?,archive_id=0,archive_text='',learn_text='' WHERE id=?",
                            (json.dumps(self.snapshot(sample)),c['id']))
            self.db.execute('DELETE FROM case_votes WHERE case_id=?',(c['id'],));self.db.commit()
            if not (sample.get('text') or sample.get('caption')):
                row=self.db.execute("SELECT observed_text FROM ai_reviews WHERE chat_id=? AND message_id=? AND label='spam' AND digest=?",(c['cid'],c['mid'],message_key(sample))).fetchone() if self.bot.ai else None
                if row:self.db.execute('UPDATE cases SET learn_text=? WHERE id=?',(row[0][:512],c['id']));self.db.commit()
        self.execute(c['id'],'admin:'+str(message['from']['id']))
        self.bot.send(message,self.number(c['id'])+' 状态：'+state_label(self.get(c['id'])['state'])+'；只有完整成功才入有效ID名单并学习独特文字。')

    def archive_observation(self,n):
        c=self.get(n)
        if not c or not c['dry']:return
        try:self.archive(c);self.archive_state(n)
        except Exception as exc:LOG.warning('observation_archive_failed case=%s type=%s',n,type(exc).__name__)

    def cancel(self,n,state='rejected'):
        self.set_state(n,state);self.refresh(n);self.archive_observation(n)

    def appeal(self,message):
        """Only self-owned real cases; no management privilege or automatic unban."""
        raw=(message.get('text') or '').split(maxsplit=2)
        uid=message.get('from',{}).get('id')
        if not raw:return
        if raw[0] in {'/start','/help'}:
            self.bot.send(message,'误封申诉：/adappeal AD-案件编号 申诉说明。只可提交本人的案件，管理员审核后处理。');return
        if raw[0]!='/adappeal':return
        match=re.fullmatch(r'AD-(\d{1,12})',raw[1]) if len(raw)>1 else None
        c=self.get(int(match[1])) if match else None
        if not c or c['uid']!=uid or c['dry'] or self.store.policy(c['cid']) is None:
            self.bot.send(message,'无法提交。请提供本人的实际处罚案件编号。');return
        if len(raw)<3 or not raw[2].strip():
            self.bot.send(message,'用法：/adappeal AD-000001 申诉说明');return
        if c['state'] not in {'banned','ban_pending_failed','unban_failed'}:
            self.bot.send(message,'此案件当前不需要封禁申诉。');return
        with self.db:
            inserted=self.db.execute('INSERT OR IGNORE INTO appeals VALUES(?,?,?,?)',
                       (c['id'],uid,int(time.time()),raw[2][:300])).rowcount
        if inserted:self.archive_state(c['id'])
        self.bot.send(message,'已提交申诉，等待管理员审核；不会自动解封。' if inserted else '此案件已提交过申诉，请等待管理员审核。')

    def unban(self,n,wrong=False,actor=None):
        c=self.get(n)
        if not c or c['dry'] or c['state'] not in {'banned','ban_pending_failed','unban_failed','unbanned'}:return
        if c['state']=='unbanned' and not wrong:return
        current=self.db.execute('SELECT case_id,active FROM active_bans WHERE cid=? AND uid=?',(c['cid'],c['uid'])).fetchone()
        newer=self.db.execute("SELECT id FROM cases WHERE cid=? AND uid=? AND id>? AND dry=0 AND state IN ('banned','ban_pending','ban_pending_failed','unban_pending','unban_failed') LIMIT 1",(c['cid'],c['uid'],n)).fetchone()
        if newer or (current and current[1] and current[0]!=n):self.set_state(n,'superseded');self.archive_state(n);return
        self.set_state(n,'unban_pending')
        try:self.api.call('unbanChatMember',chat_id=c['cid'],user_id=c['uid'],only_if_banned=True)
        except Exception:
            self.set_state(n,'unban_failed');self.archive_state(n)
            if self.bot.management:self.bot.management.audit(c['cid'],actor or self.bot.owner,'wrong_failed' if wrong else 'unban_failed',c['uid'],n)
            return
        cancelled=[row[0] for row in self.db.execute("SELECT id FROM cases WHERE cid=? AND uid=? AND state IN ('pending','held')",(c['cid'],c['uid']))]
        try:
            with self.db:
                self.db.execute('UPDATE active_bans SET active=0 WHERE cid=? AND uid=?',(c['cid'],c['uid']))
                if wrong:self.db.execute('UPDATE case_samples SET active=0 WHERE case_id=?',(n,))
                self.db.execute("UPDATE cases SET state='cancelled_unban' WHERE cid=? AND uid=? AND state IN ('pending','held')",(c['cid'],c['uid']))
                self.db.execute('UPDATE cases SET state=? WHERE id=?',('wrong_unbanned' if wrong else 'unbanned',n))
        except Exception as exc:
            self.set_state(n,'unban_failed');self.archive_state(n)
            LOG.warning('unban_metadata_failed case=%s type=%s',n,type(exc).__name__);return
        self.archive_state(n);self.refresh(n)
        for other in cancelled:self.refresh(other);self.archive_state(other)
        if self.bot.management:
            try:self.bot.management.audit(c['cid'],actor or self.bot.owner,'wrong' if wrong else 'unban',c['uid'],n)
            except Exception as exc:
                self.db.rollback();LOG.warning('unban_audit_failed case=%s type=%s',n,type(exc).__name__)

    def callback(self,q):
        match=re.fullmatch(r'adcase:(vote|reject|ban|unban|wrong|info):(\d+)',str(q.get('data','')))
        if not match:return
        c=self.get(int(match[2]));uid=q.get('from',{}).get('id');origin=q.get('message',{});where=origin.get('chat',{}).get('id')
        def ack(text):
            try:self.api.call('answerCallbackQuery',callback_query_id=q['id'],text=text[:180],show_alert=True)
            except Exception as exc:
                if getattr(exc,'kind',None)!='expired_callback':raise
                LOG.warning('case_callback_notice_expired case=%s action=%s',c['id'] if c else 0,match[1])
        if not c or self.store.policy(c['cid']) is None:ack('案件无效或源群未授权');return
        action=match[1]
        if action in {'unban','wrong','info'}:
            if where!=self.channel or origin.get('message_id')!=c['archive_id']:ack('不是原归档记录');return
            if uid!=self.bot.owner and self.api.call('getChatMember',chat_id=self.channel,user_id=uid).get('status') not in ADMIN:ack('仅主人或归档频道管理员可用');return
            ack(self.number(c['id'])+' 当前状态：'+state_label(c['state']))
            if action!='info':self.unban(c['id'],wrong=action=='wrong',actor=uid)
            return
        if where!=c['cid'] or origin.get('message_id')!=c['report_id']:ack('不是原群复核消息');return
        if c['state'] not in {'pending','held'}:ack('案件已经处理，不重复操作');return
        if uid==c['uid'] and not c['dry']:ack('不能给自己驳回');return
        role=self.api.call('getChatMember',chat_id=c['cid'],user_id=uid).get('status')
        if role not in {'member','restricted','administrator','creator'}:ack('仅本群成员可以操作');return
        if action=='vote':
            self.db.execute('INSERT OR IGNORE INTO case_votes VALUES(?,?)',(c['id'],uid));self.db.commit()
            count=self.db.execute('SELECT COUNT(*) FROM case_votes WHERE case_id=?',(c['id'],)).fetchone()[0]
            ack('驳回票：'+str(count)+'/3，每人一票')
            if count>=3:self.cancel(c['id'],'member_rejected')
            else:self.refresh(c['id'])
        elif role not in ADMIN and uid!=self.bot.owner:ack('仅本群群主或管理员可操作')
        else:
            ack('已提交，'+('观察测试，不会真的处罚' if c['dry'] else '执行前重新核查权限'))
            if action=='reject':self.cancel(c['id'],'admin_rejected')
            elif action=='ban':self.execute(c['id'],'admin:'+str(uid))

    def cleanup_notices(self,now):
        rows=self.db.execute("SELECT case_id,cid,mid,attempts FROM case_notice_cleanup WHERE state='pending' AND due<=? ORDER BY due LIMIT 5",(now,)).fetchall()
        for n,cid,mid,attempts in rows:
            try:
                # Only the stored bot notice, never the user's original or channel archive.
                self.api.call('deleteMessage',chat_id=cid,message_id=mid)
                self.db.execute("UPDATE case_notice_cleanup SET state='done' WHERE case_id=?",(n,))
            except Exception as exc:
                code=getattr(exc,'code',0)
                terminal=code in {400,403} or attempts>=4
                self.db.execute("UPDATE case_notice_cleanup SET attempts=attempts+1,state=?,due=? WHERE case_id=?",('failed' if terminal else 'pending',now+max(30,min(getattr(exc,'retry_after',0),300)),n))
                LOG.warning('case_notice_cleanup_failed case=%s code=%s terminal=%s',n,code,terminal)
            self.db.commit()

    def tick(self):
        now=int(time.time())
        self.cleanup_notices(now)
        rows=self.db.execute("SELECT id,deadline,dry,cid FROM cases WHERE state='pending' AND deadline>0 AND deadline<=? ORDER BY deadline LIMIT 5",(now,)).fetchall()
        for n,deadline,dry,cid in rows:
            policy=self.store.policy(cid)
            if not dry and (not policy or policy['mode']!='review'):self.cancel(n,'mode_cancelled')
            elif now-deadline>120:
                self.set_state(n,'held');self.db.execute('UPDATE cases SET deadline=0 WHERE id=?',(n,));self.db.commit();self.refresh(n)
            else:self.execute(n,'timeout')
        # Keep ID/history metadata; old archived/closed raw bodies have a bounded local lifetime.
        self.db.execute("UPDATE cases SET payload='{}',learn_text='' WHERE created<? AND state NOT IN ('pending','held','preparing','archiving','delete_pending','ban_pending') AND id NOT IN (SELECT id FROM cases ORDER BY id DESC LIMIT 1000)",(now-90*86400,));self.db.commit()

    def status(self,cid=None):
        clause=' AND cid=?' if cid is not None else '';params=(cid,) if cid is not None else ()
        pending=self.db.execute("SELECT COUNT(*) FROM cases WHERE state IN ('pending','held')"+clause,params).fetchone()[0]
        active=self.db.execute('SELECT COUNT(*) FROM active_bans WHERE active=1'+clause,params).fetchone()[0]
        learned=self.db.execute('SELECT COUNT(*) FROM case_samples WHERE active=1'+clause,params).fetchone()[0]
        return '复核案件：'+str(pending)+'；有效ID记录：'+str(active)+'；案件学习样本：'+str(learned)+'\n归档频道：'+str(self.channel)+'\n观察模式按钮只模拟；review模式才执行自动处罚。'
