"""AI spam triggers temporary containment, never an automatic permanent ban."""
import unittest
from unittest.mock import patch
from rules import classify
from test_cases import CID, sample
import test_quarantine as fixture


class AIContainmentTests(unittest.TestCase):
    setUp=fixture.QuarantineTests.setUp
    tearDown=fixture.QuarantineTests.tearDown
    callback=fixture.QuarantineTests.callback
    methods=fixture.QuarantineTests.methods

    def ai(self,message=None,label='spam'):
        m=message or sample(text='神秘暗号去看看')
        self.cases.track(m)
        self.cases.ai_result(m,{'label':label,'reason':'广告招募','observed_text':''})
        return self.store.db.execute('select * from cases order by id desc limit 1').fetchone()

    def test_ai_only_spam_mutes_deletes_and_keeps_review_without_ban(self):
        self.assertEqual(classify(sample(text='神秘暗号去看看'),{})['level'],'clean')
        c=self.ai();q=self.cases.quarantine.get(c['id'])
        self.assertEqual(q['phase'],'muted');self.assertEqual(q['deleted'],1)
        self.assertEqual(c['state'],'pending');self.assertEqual(c['deadline'],0)
        self.assertNotIn('banChatMember',self.methods())
        self.assertEqual(self.store.db.execute('select count(*) from case_samples').fetchone()[0],0)
        self.assertEqual(self.store.db.execute('select count(*) from active_bans').fetchone()[0],0)
        with patch('cases.time.time',return_value=q['until_ts']+1):self.cases.tick()
        self.assertEqual(self.cases.get(c['id'])['state'],'pending')
        self.assertNotIn('banChatMember',self.methods())

    def test_existing_weak_card_upgraded_and_duplicate_result_does_not_repeat(self):
        m=sample(text='神秘暗号去看看');self.cases.track(m)
        old=self.cases.open(m,'弱规则待复核',eligible=False,dry=False)
        self.cases.ai_result(m,{'label':'spam','reason':'广告'})
        self.cases.ai_result(m,{'label':'spam','reason':'广告'})
        self.assertEqual(self.store.db.execute('select count(*) from cases').fetchone()[0],1)
        self.assertEqual(self.cases.get(old['id'])['report_id'],old['report_id'])
        self.assertEqual(self.methods().count('restrictChatMember'),1)
        self.assertEqual(self.methods().count('deleteMessage'),1)

    def test_error_uncertain_and_ham_never_contain(self):
        for mid,label in ((7,'error'),(8,'uncertain'),(9,'ham')):
            self.ai({**sample(),'message_id':mid},label)
        self.assertNotIn('restrictChatMember',self.methods())
        self.assertNotIn('deleteMessage',self.methods())
        self.assertNotIn('banChatMember',self.methods())

    def test_ai_only_admin_owner_and_whitelist_remain_protected(self):
        self.bot.management.change_white(CID,3,1,True)
        for uid in (1,20,3):self.ai(sample(uid=uid,text='神秘暗号去看看'))
        self.assertNotIn('restrictChatMember',self.methods())
        self.assertNotIn('deleteMessage',self.methods())

    def test_confirmation_required_for_permanent_ban(self):
        c=dict(self.ai());self.callback(c,'ban')
        self.assertEqual(self.cases.get(c['id'])['state'],'banned')
        self.assertLess(self.methods().index('copyMessage'),self.methods().index('banChatMember'))
        self.assertEqual(self.methods().count('banChatMember'),1)

    def test_rejection_restores_ai_only_mute(self):
        c=dict(self.ai());self.callback(c,'reject')
        self.assertEqual(self.cases.get(c['id'])['state'],'admin_rejected')
        self.assertEqual(self.cases.quarantine.get(c['id'])['phase'],'released')
        self.assertNotIn('banChatMember',self.methods())

    def test_stale_edited_message_not_contained(self):
        m=sample(text='神秘暗号去看看');self.cases.track({**m,'text':'已编辑'})
        self.cases.ai_result(m,{'label':'spam','reason':'广告'})
        self.assertNotIn('restrictChatMember',self.methods())
        self.assertNotIn('deleteMessage',self.methods())

    def test_observation_mode_not_contained(self):
        p=self.store.policy(CID);p['mode']='observe';self.store.set('group:'+str(CID),p)
        self.ai()
        self.assertNotIn('restrictChatMember',self.methods())
        self.assertNotIn('deleteMessage',self.methods())

    def test_failed_mute_does_not_delete(self):
        self.api.fail='restrictChatMember';c=self.ai()
        self.assertEqual(self.cases.quarantine.get(c['id'])['phase'],'mute_uncertain')
        self.assertNotIn('deleteMessage',self.methods())
        self.assertNotIn('banChatMember',self.methods())

if __name__=='__main__':unittest.main()
