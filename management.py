"""Scoped moderation controls and durable change history; numeric IDs only."""
import re
import secrets
import time
from cases import state_label

COMMANDS={'/admanage','/adsettings','/adblacklist','/adhistory','/adunban','/adwrong','/adwhite','/adwhitelist','/adgwhite','/adaudit'}
MENU=[{'command':name[1:],'description':description} for name,description in (
    ('/admanage','打开本群名单和案件管理面板'),('/adsettings','本群检测功能开关'),('/adblacklist','本群有效封禁名单，支持页码'),
    ('/adhistory','本群案件历史；可输入用户ID'),('/adunban','解除案件封禁，保留学习样本'),
    ('/adwrong','纠正案件误封并撤回该案件学习'),('/adwhite','本群白名单 add/remove 用户ID'),
    ('/adwhitelist','本群和全局白名单，支持页码'),('/adaudit','查看本群名单和解封变更记录'))]

class Management:
    def __init__(self,bot):
        self.bot,self.api,self.db=bot,bot.api,bot.store.db
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS whitelists(scope INTEGER,uid INTEGER,actor INTEGER,updated INTEGER,
            PRIMARY KEY(scope,uid));
          CREATE TABLE IF NOT EXISTS management_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,ts INTEGER,
            scope INTEGER,actor INTEGER,action TEXT,target INTEGER,case_id INTEGER DEFAULT 0);
          CREATE TABLE IF NOT EXISTS management_panels(token TEXT PRIMARY KEY,cid INTEGER,origin INTEGER,
            mid INTEGER,created INTEGER);
        ''');self.db.commit()

    def whitelisted(self,cid,uid):
        return bool(uid and self.db.execute('SELECT 1 FROM whitelists WHERE uid=? AND scope IN (?,0)',(uid,cid)).fetchone())

    def audit(self,scope,actor,action,target=0,case_id=0):
        self.db.execute('INSERT INTO management_audit(ts,scope,actor,action,target,case_id) VALUES(?,?,?,?,?,?)',
            (int(time.time()),scope,actor,action,target,case_id));self.db.commit()

    def authorized(self,cid,uid):
        return self.bot.store.policy(cid) is not None and (uid==self.bot.owner or self.bot.admin(cid,uid))

    def target(self,args,message):
        if args and re.fullmatch(r'[1-9]\d{0,15}',args[0]):return int(args[0])
        if not args:
            sample=message.get('reply_to_message',{})
            if sample.get('sender_chat'):return None
            if sample.get('chat',{}).get('id')!=message['chat']['id']:return None
            uid=sample.get('from',{}).get('id')
            return uid if isinstance(uid,int) and uid>0 else None
        return None

    def change_white(self,scope,uid,actor,add):
        if add:
            if self.db.execute('SELECT 1 FROM active_bans WHERE uid=? AND active=1'+(' AND cid=?' if scope else ''),
                (uid,scope) if scope else (uid,)).fetchone():
                return '该ID仍在有效封禁名单，请先纠正误封或解除封禁，再加入白名单。'
            count=self.db.execute('SELECT COUNT(*) FROM whitelists WHERE scope=?',(scope,)).fetchone()[0]
            if count>=1000 and not self.db.execute('SELECT 1 FROM whitelists WHERE scope=? AND uid=?',(scope,uid)).fetchone():
                return '此范围白名单已达1000条上限。'
        with self.db:
            if add:self.db.execute('INSERT OR REPLACE INTO whitelists VALUES(?,?,?,?)',(scope,uid,actor,int(time.time())))
            else:self.db.execute('DELETE FROM whitelists WHERE scope=? AND uid=?',(scope,uid))
            self.audit(scope,actor,'white_add' if add else 'white_remove',uid)
        if add:
            query="SELECT id FROM cases WHERE uid=? AND dry=0 AND state IN ('pending','held')"+(' AND cid=?' if scope else '')
            ids=[row[0] for row in self.db.execute(query,(uid,scope) if scope else (uid,))]
            for n in ids:
                self.bot.cases.cancel(n,'whitelisted');self.bot.cases.archive_state(n)
        return ('已加入' if add else '已移除')+('全局' if scope==0 else '本群')+'白名单\n用户ID：'+str(uid)+('\n全局白名单仍生效。' if scope and not add and self.whitelisted(scope,uid) else '')

    def page(self,args):
        if not args:return 1
        if len(args)!=1 or not re.fullmatch(r'[1-9]\d{0,5}',args[0]):raise ValueError('页码必须是正整数')
        return int(args[0])

    def listing(self,cid,view,page=1,uid=None):
        page=max(1,page);skip=(page-1)*8;rows=[];lines=['群ID：'+str(cid)]
        if view=='settings':
            from policy import features, LABELS
            p=self.bot.store.policy(cid)
            lines+=['本群检测设置；模式：'+p['mode']]
            lines += [LABELS[k]+'：'+('开启' if v else '关闭') for k,v in features(p).items()]
            lines+=['开关不改变处罚模式、权限保护或域名黑名单。',
                    '关闭文字规则不关闭AI文字理解；关闭照片只关闭画面识别，仍检查说明。',
                    'AI故障、不确定和重复信号不足时，不自动封禁。']
            return '\n'.join(lines),False,[]
        if view=='black':
            lines.append('本群有效封禁名单（第'+str(page)+'页）')
            rows=self.db.execute('SELECT uid,case_id FROM active_bans WHERE cid=? AND active=1 ORDER BY case_id DESC LIMIT 9 OFFSET ?',(cid,skip)).fetchall()
            for r in rows[:8]:lines.append('用户ID：'+str(r[0])+' 案件 '+self.bot.cases.number(r[1]))
        elif view=='history':
            lines.append('本群案件历史'+(' 用户ID：'+str(uid) if uid else '')+'（第'+str(page)+'页）')
            sql='SELECT id,uid,state,created,archive_id,dry FROM cases WHERE cid=?'+(' AND uid=?' if uid else '')+' ORDER BY id DESC LIMIT 9 OFFSET ?'
            rows=self.db.execute(sql,(cid,uid,skip) if uid else (cid,skip)).fetchall()
            for r in rows[:8]:
                lines.append(self.bot.cases.number(r[0])+' 用户ID：'+str(r[1])+' '+state_label(r[2])+('［观察］' if r[5] else ''))
                if r[4]:lines.append('https://t.me/c/'+str(abs(self.bot.cases.channel))[3:]+'/'+str(r[4]))
        elif view=='white':
            lines.append('有效白名单（第'+str(page)+'页；本群与全局并列）')
            rows=self.db.execute('SELECT scope,uid FROM whitelists WHERE scope IN (?,0) ORDER BY scope,uid LIMIT 9 OFFSET ?',(cid,skip)).fetchall()
            for r in rows[:8]:lines.append(('全局' if r[0]==0 else '本群')+' 用户ID：'+str(r[1]))
        elif view=='audit':
            lines.append('变更记录（第'+str(page)+'页；含全局白名单变更）')
            rows=self.db.execute('SELECT ts,actor,action,target,case_id FROM management_audit WHERE scope IN (?,0) ORDER BY id DESC LIMIT 9 OFFSET ?',(cid,skip)).fetchall()
            labels={'white_add':'加入白名单','white_remove':'移除白名单','unban':'解封','wrong':'纠正误封','unban_failed':'解封失败或未完成','wrong_failed':'纠正未完成'}
            for r in rows[:8]:lines.append(time.strftime('%m-%d %H:%M',time.gmtime(r[0]+28800))+' '+labels.get(r[2],r[2])+' 操作人：管理员（ID仅后台审计） 用户ID：'+str(r[3])+(' '+self.bot.cases.number(r[4]) if r[4] else ''))
        else:
            counts=[self.db.execute(sql,(cid,)).fetchone()[0] for sql in (
                'SELECT COUNT(*) FROM active_bans WHERE cid=? AND active=1',
                'SELECT COUNT(*) FROM whitelists WHERE scope=?',
                'SELECT COUNT(*) FROM cases WHERE cid=?')]
            lines+=['本群管理面板','有效封禁：'+str(counts[0])+'；本群白名单：'+str(counts[1])+'；案件：'+str(counts[2]),
                '/adhistory 用户ID 按成员查案件','/adwhite add 用户ID 本群豁免','/adwhite remove 用户ID 移除本群豁免',
                '/adunban AD-000001 解封，保留样本','/adwrong AD-000001 纠正误封并撤回本案样本',
                '白名单不授予管理权限；全局白名单仅主人可修改。']
            return '\n'.join(lines),False,[]
        if not rows:lines.append('没有记录。')
        cases=[r[0] if view=='history' else r[1] for r in rows[:8]] if view in {'history','black'} else []
        return '\n'.join(lines),len(rows)>8,cases

    def keyboard(self,token,view,page,more,case_ids):
        def button(text,action):return {'text':text,'callback_data':'adm:'+token+':'+action}
        rows=[[button('黑名单','black:1'),button('案件历史','history:1')],
              [button('白名单','white:1'),button('变更记录','audit:1')]]
        rows.append([button('检测设置','settings:1')])
        if view=='settings':
            from policy import DEFAULTS, LABELS
            rows += [[button('切换 '+LABELS[k],'toggle:'+str(i))] for i,k in enumerate(DEFAULTS)]
        for n in case_ids:rows.append([button(self.bot.cases.number(n)+' 详情','case:'+str(n))])
        nav=[]
        if page>1:nav.append(button('上一页',view+':'+str(page-1)))
        if more:nav.append(button('下一页',view+':'+str(page+1)))
        if nav:rows.append(nav)
        rows.append([button('返回面板','home:1')]);return {'inline_keyboard':rows}

    def panel(self,message,cid,view='home',page=1):
        text,more,ids=self.listing(cid,view,page);token=secrets.token_hex(6)
        posted=self.api.call('sendMessage',chat_id=message['chat']['id'],text=text,
            reply_markup=self.keyboard(token,view,page,more,ids),link_preview_options={'is_disabled':True})
        self.db.execute('INSERT INTO management_panels VALUES(?,?,?,?,?)',(token,cid,message['chat']['id'],posted['message_id'],int(time.time())))
        self.db.execute('DELETE FROM management_panels WHERE created<?',(int(time.time())-86400,));self.db.commit()

    def command(self,message,raw):
        name=raw[0].split('@')[0];uid=message['from']['id'];cid=message['chat']['id'];private=message['chat'].get('type')=='private'
        if message.get('sender_chat') or (private and uid!=self.bot.owner):return False
        if not private and self.bot.store.policy(cid) is None:return False
        role={'status':'creator'} if uid==self.bot.owner else self.api.call('getChatMember',chat_id=cid,user_id=uid)
        if role.get('status') not in {'creator','administrator'}:return False
        if name in {'/adunban','/adwrong'} and not self.bot.moderation_role(role):
            self.bot.send(message,'需要本群限制成员权限。');return True
        if name=='/adwhite' and len(raw)>1 and role.get('status')!='creator':
            self.bot.send(message,'仅机器人主人或本群群主可以修改白名单。');return True
        args=raw[1:]
        if name=='/adgwhite':
            if uid!=self.bot.owner:self.bot.send(message,'仅主人可以修改全局白名单。');return True
            scope=0
        elif private:
            if name in {'/admanage','/adsettings'}:
                targets=list(self.bot.store.config['groups'])
                view='settings' if name=='/adsettings' else 'home'
                if len(args)==1 and args[0] in targets:self.panel(message,int(args[0]),view)
                elif len(targets)==1 and not args:self.panel(message,int(targets[0]),view)
                else:self.bot.send(message,'选择目标群，发送 /admanage 群ID\n'+'\n'.join('群ID：'+t for t in targets))
            else:self.bot.send(message,'请在目标群执行，或私聊 /admanage 打开管理面板；全局白名单用 /adgwhite。')
            return True
        else:scope=cid
        if name in {'/adwhite','/adgwhite'}:
            if not args:
                if scope==0:
                    rows=self.db.execute('SELECT uid FROM whitelists WHERE scope=0 ORDER BY uid LIMIT 30').fetchall()
                    self.bot.send(message,'全局白名单（最多显示30条）\n'+('\n'.join('用户ID：'+str(r[0]) for r in rows) or '没有记录。')+'\n/adgwhite add 用户ID\n/adgwhite remove 用户ID')
                else:self.panel(message,cid,'white')
                return True
            target=self.target(args[1:],message)
            if args[0] not in {'add','remove'} or not target or len(args)>2:
                self.bot.send(message,'用法：'+name+' add/remove 用户ID；也可回复成员消息省略ID。');return True
            self.bot.send(message,self.change_white(scope,target,uid,args[0]=='add'));return True
        if name in {'/adunban','/adwrong'}:
            if len(args)!=1 or not re.fullmatch(r'AD-\d{1,12}',args[0]):self.bot.send(message,'用法：'+name+' AD-000001');return True
            c=self.bot.cases.get(int(args[0][3:]))
            if not c or c['cid']!=cid:self.bot.send(message,'没有这个本群案件。');return True
            if c['dry']:self.bot.send(message,'这是观察测试，没有实际封禁，无需解封。');return True
            self.bot.cases.unban(c['id'],wrong=name=='/adwrong',actor=uid)
            self.bot.send(message,args[0]+' 状态：'+state_label(self.bot.cases.get(c['id'])['state']));return True
        if name=='/adhistory' and args:
            target=self.target(args[:1],message)
            try:page=self.page(args[1:])
            except ValueError:page=0
            if len(args)>2 or not target or not page:self.bot.send(message,'用法：/adhistory 或 /adhistory 用户ID [页码]');return True
            text,more,_=self.listing(cid,'history',page,uid=target)
            self.bot.send(message,text+('\n下一页：/adhistory '+str(target)+' '+str(page+1) if more else ''));return True
        view={'/admanage':'home','/adsettings':'settings','/adblacklist':'black','/adhistory':'history','/adwhitelist':'white','/adaudit':'audit'}.get(name)
        if view:
            try:page=self.page(args)
            except ValueError:self.bot.send(message,'页码必须是正整数。');return True
            self.panel(message,cid,view,page);return True
        return False

    def callback(self,q):
        match=re.fullmatch(r'adm:([a-f0-9]{12}):(home|black|history|white|audit|settings|toggle|case|unban|wrong|confirm|reject):(\d{1,12})',str(q.get('data','')))
        if not match:return
        token,action,value=match.groups();value=int(value);uid=q.get('from',{}).get('id');origin=q.get('message',{})
        def ack(text):
            try:self.api.call('answerCallbackQuery',callback_query_id=q['id'],text=text[:180],show_alert=True)
            except Exception as exc:
                if getattr(exc,'kind',None)!='expired_callback':raise
        p=self.db.execute('SELECT cid,origin,mid,created FROM management_panels WHERE token=?',(token,)).fetchone()
        if not p or p[3]<time.time()-86400:ack('面板已过期，请重新 /admanage');return
        cid,where,mid,_=p
        if origin.get('chat',{}).get('id')!=where or origin.get('message_id')!=mid:ack('不是原管理面板');return
        if (where!=cid and not (where==self.bot.owner and uid==self.bot.owner)) or not self.authorized(cid,uid):ack('仅主人或本群管理员可操作');return
        if action=='toggle' and not self.bot.can_configure(cid,uid):ack('仅机器人主人或本群群主可以修改开关');return
        if action in {'unban','wrong','confirm','reject'} and not self.bot.can_moderate(cid,uid):ack('需要本群限制成员权限');return
        ack('正在处理')
        if action=='toggle':
            from policy import DEFAULTS, features
            if value>=len(DEFAULTS):return
            key=list(DEFAULTS)[value];policy=self.bot.store.policy(cid)
            switches=features(policy);switches[key]=not switches[key];policy['features']=switches
            self.bot.store.set('group:'+str(cid),policy)
            self.audit(cid,uid,'feature_'+key+'_'+('on' if switches[key] else 'off'))
            action,value='settings',1
        if action in {'case','unban','wrong','confirm','reject'}:
            c=self.bot.cases.get(value)
            if not c or c['cid']!=cid:return
            if action in {'confirm','reject'}:
                if not self.bot.cases.quarantine or c['state'] not in {'pending','held','ban_pending_failed'}:return
                if action=='confirm':self.bot.cases.execute(value,'admin:'+str(uid))
                elif c['state']!='ban_pending_failed':self.bot.cases.cancel(value,'admin_rejected')
            elif action!='case' and not c['dry']:self.bot.cases.unban(value,wrong=action=='wrong',actor=uid)
            c=self.bot.cases.get(value);text=self.bot.cases.text(c)
            rows=[]
            if self.bot.cases.quarantine and not c['dry'] and c['state'] in {'pending','held','ban_pending_failed'}:
                rows=[[{'text':'确认广告并永久封禁','callback_data':'adm:'+token+':confirm:'+str(value)}],
                      [{'text':'管理驳回，撤销本Bot限制','callback_data':'adm:'+token+':reject:'+str(value)}]]
            if c['state']=='ban_pending_failed':rows=rows[:1]
            if not c['dry'] and c['state'] in {'banned','ban_pending_failed','unban_failed','unbanned'}:
                rows+=[[{'text':'解除本群封禁（保留样本）','callback_data':'adm:'+token+':unban:'+str(value)}],
                      [{'text':'纠正误封并撤回本案学习','callback_data':'adm:'+token+':wrong:'+str(value)}]]
            if c['archive_id']:text+='\n归档：https://t.me/c/'+str(abs(self.bot.cases.channel))[3:]+'/'+str(c['archive_id'])
            rows.append([{'text':'返回面板','callback_data':'adm:'+token+':home:1'}]);markup={'inline_keyboard':rows}
        else:
            text,more,ids=self.listing(cid,action,value);markup=self.keyboard(token,action,value,more,ids)
        self.api.call('editMessageText',chat_id=where,message_id=mid,text=text,reply_markup=markup,link_preview_options={'is_disabled':True})
