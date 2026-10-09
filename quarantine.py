"""Temporary containment; permanent bans always require an administrator decision."""
import json
import logging
import time

LOG=logging.getLogger('ad-killer')
PERMISSIONS=('can_send_messages','can_send_audios','can_send_documents','can_send_photos',
             'can_send_videos','can_send_video_notes','can_send_voice_notes','can_send_polls',
             'can_send_other_messages','can_add_web_page_previews','can_change_info',
             'can_invite_users','can_pin_messages','can_manage_topics')

class Quarantine:
    def __init__(self,cases):
        self.cases=cases;self.db=cases.db;self.api=cases.api
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS quarantines(case_id INTEGER PRIMARY KEY,phase TEXT,until_ts INTEGER,
            prior TEXT,deleted INTEGER DEFAULT 0,notice_id INTEGER DEFAULT 0,notice_phase TEXT DEFAULT 'new');
          CREATE TABLE IF NOT EXISTS quarantine_cleanup(cid INTEGER,mid INTEGER,due INTEGER,
            attempts INTEGER DEFAULT 0,state TEXT DEFAULT 'pending',PRIMARY KEY(cid,mid));
        ''')
        self.db.commit()

    def get(self,n):
        row=self.db.execute('SELECT * FROM quarantines WHERE case_id=?',(n,)).fetchone()
        return dict(row) if row else None

    def phase(self,n,value):
        self.db.execute('UPDATE quarantines SET phase=? WHERE case_id=?',(value,n));self.db.commit()

    def start(self,n):
        c=self.cases.get(n);m=json.loads(c['payload'])
        if self.get(n) or c['dry']:return
        try:
            if self.cases.local_protected(m):return
            self.cases.ready(c['cid'])
            member=self.api.call('getChatMember',chat_id=c['cid'],user_id=c['uid'])
            if member.get('status') not in {'member','restricted'}:return
            until=int(time.time())+86400
            # Do not overwrite a restriction issued by another administrator.
            phase='external' if member['status']=='restricted' else 'mute_pending'
            self.db.execute('INSERT INTO quarantines(case_id,phase,until_ts,prior) VALUES(?,?,?,?)',
                            (n,phase,until,json.dumps(member)));self.db.commit()
            if phase=='mute_pending':
                # Use the fresh member response above; no network action has intervened.
                # Recheck from Telegram again after the restriction, before deleting.
                if self.cases.local_protected(m):self.phase(n,'protected');return
                self.api.call('restrictChatMember',chat_id=c['cid'],user_id=c['uid'],
                              permissions={key:False for key in PERMISSIONS},
                              use_independent_chat_permissions=True,until_date=until)
                self.phase(n,'muted')
            if self.cases.protected(m):return
            try:
                self.api.call('deleteMessage',chat_id=c['cid'],message_id=c['mid'])
                deleted=1
            except Exception as exc:
                if getattr(exc,'kind',None)!='message_missing':raise
                deleted=2
            self.db.execute('UPDATE quarantines SET deleted=? WHERE case_id=?',(deleted,n));self.db.commit()
            self.cases.store.event(m,{'level':'suspected','reason':'pending_admin_review'},'case_quarantined')
        except Exception as exc:
            self.db.rollback()
            if self.get(n) and self.get(n)['phase']=='mute_pending':self.phase(n,'mute_uncertain')
            LOG.warning('quarantine_failed case=%s type=%s',n,type(exc).__name__)

    def detail(self,n):
        q=self.get(n)
        if not q:return '未提前删帖或禁言（证据不足或仅人工复核）。'
        deleted={0:'未确认删除',1:'已删除',2:'原消息已不存在，删除者未知'}[q['deleted']]
        labels={'muted':'已临时禁言，最长24小时','external':'原有权限限制保留，未覆盖',
                'released':'已撤销本Bot禁言','expired':'临时禁言期限已到',
                'converted':'已转为永久封禁','mute_uncertain':'禁言结果不确定，需管理员核查',
                'mute_pending':'禁言调用可能中断，需管理员核查',
                'release_pending':'解除调用可能中断，需管理员核查',
                'release_failed':'解除失败，管理员可再次驳回重试',
                'release_conflict':'权限或关联案件有变化，未覆盖，请管理员核查',
                'protected':'目标受身份保护，已停止'}
        return '原消息：'+deleted+'；发言权限：'+labels.get(q['phase'],q['phase'])

    def release(self,n):
        q=self.get(n);c=self.cases.get(n)
        if not q or q['phase'] in {'external','released','expired','converted','protected'}:return True
        try:
            current=self.api.call('getChatMember',chat_id=c['cid'],user_id=c['uid'])
            if current.get('status')=='member':self.phase(n,'released');return True
            sibling=self.db.execute("SELECT 1 FROM cases WHERE cid=? AND uid=? AND id!=? AND dry=0 AND state IN ('pending','held') LIMIT 1",(c['cid'],c['uid'],n)).fetchone()
            if (sibling or current.get('status')!='restricted' or
                current.get('until_date')!=q['until_ts'] or
                any(current.get(key,False) for key in PERMISSIONS)):
                self.phase(n,'release_conflict');return False
            defaults=self.api.call('getChat',chat_id=c['cid']).get('permissions')
            if not isinstance(defaults,dict) or 'can_send_messages' not in defaults:
                raise RuntimeError('group_permissions_unavailable')
            self.phase(n,'release_pending')
            self.api.call('restrictChatMember',chat_id=c['cid'],user_id=c['uid'],
                          permissions={key:bool(defaults.get(key,False)) for key in PERMISSIONS},
                          use_independent_chat_permissions=True,until_date=0)
            self.phase(n,'released');return True
        except Exception as exc:
            self.phase(n,'release_failed');LOG.warning('quarantine_release_failed case=%s type=%s',n,type(exc).__name__);return False

    def finalize(self,n):
        c=self.cases.get(n);q=self.get(n)
        if not q:
            self.db.execute("INSERT INTO quarantines(case_id,phase,until_ts,prior) VALUES(?,'converted',0,'{}')",(n,));self.db.commit()
            q=self.get(n)
        self.phase(n,'converted')
        if c['report_id']:
            self.schedule(c['cid'],c['report_id'],int(time.time()))
        # Persist intent before sending; a timeout must not generate duplicate notifications.
        if q['notice_phase']=='new':
            self.db.execute("UPDATE quarantines SET notice_phase='sending' WHERE case_id=?",(n,));self.db.commit()
            try:
                text=self.cases.number(n)+' 已永久封禁广告成员\n用户ID：'+str(c['uid'])+'\n证据和误判解封入口已归档到广告频道。'
                posted=self.api.call('sendMessage',chat_id=c['cid'],text=text,link_preview_options={'is_disabled':True})
                self.db.execute("UPDATE quarantines SET notice_id=?,notice_phase='sent' WHERE case_id=?",(posted['message_id'],n));self.db.commit()
                self.schedule(c['cid'],posted['message_id'],int(time.time())+180)
            except Exception as exc:LOG.warning('ban_notice_failed case=%s type=%s',n,type(exc).__name__)
        self.cleanup(int(time.time()))

    def schedule(self,cid,mid,due):
        self.db.execute('INSERT OR IGNORE INTO quarantine_cleanup(cid,mid,due) VALUES(?,?,?)',(cid,mid,due));self.db.commit()

    def cleanup(self,now):
        rows=self.db.execute("SELECT cid,mid,attempts FROM quarantine_cleanup WHERE state='pending' AND due<=? ORDER BY due LIMIT 5",(now,)).fetchall()
        for cid,mid,attempts in rows:
            try:
                self.api.call('deleteMessage',chat_id=cid,message_id=mid)
                self.db.execute("UPDATE quarantine_cleanup SET state='done' WHERE cid=? AND mid=?",(cid,mid))
            except Exception as exc:
                if getattr(exc,'kind',None)=='message_missing':state='done'
                else:state='failed' if getattr(exc,'code',0) in {400,403} or attempts>=4 else 'pending'
                self.db.execute('UPDATE quarantine_cleanup SET state=?,attempts=attempts+1,due=? WHERE cid=? AND mid=?',
                                (state,now+max(30,min(getattr(exc,'retry_after',0),300)),cid,mid))
                LOG.warning('quarantine_notice_cleanup case=%s state=%s',mid,state)
            self.db.commit()

    def tick(self,now):
        self.cleanup(now)
        # Telegram's finite restriction expires without a network call, including while offline.
        rows=self.db.execute("SELECT case_id FROM quarantines WHERE phase IN ('muted','mute_pending','mute_uncertain') AND until_ts<=?",(now,)).fetchall()
        for row in rows:
            self.phase(row[0],'expired');self.cases.refresh(row[0])
