"""Per-group feature switches and bounded repeat evidence. No moderation calls."""
import hashlib
import time
from rules import classify, fingerprint, extract_links

DEFAULTS = {'text': True, 'photo': True, 'behavior': True, 'bot': True,
            'repeat': True, 'ai': True}
LABELS = {'text': '文字规则', 'photo': '照片视觉', 'behavior': '转发/引用/名片/定位',
          'bot': '机器人引流规则', 'repeat': '重复推广', 'ai': 'AI复核'}

def features(policy):
    return {key: policy.get('features', {}).get(key, default) for key, default in DEFAULTS.items()}

def filtered(message, policy):
    switches = features(policy)
    result = dict(message)
    if not switches['photo']: result.pop('photo', None)
    if not switches['behavior']:
        for key in ('forward_origin', 'external_reply', 'contact', 'location'): result.pop(key, None)
    return result

def verdict(message, policy):
    result = classify(filtered(message, policy), policy)
    # Explicit domain rules always remain effective. Other switches are category-specific.
    reason = result['reason']
    switches = features(policy)
    if reason in {'bot_promotion', 'listed_bot'} and not switches['bot']:
        result = classify(filtered(message, policy), {**policy, 'disable_bot_rules': True})
    if reason == 'behavior_and_promotion' and not switches['behavior']:
        return {'level': 'clean', 'reason': 'no_match', 'domains': []}
    if not switches['text'] and result['reason'] not in {'blocked_domain', 'bot_promotion', 'listed_bot', 'behavior_and_promotion'}:
        return {'level': 'clean', 'reason': 'no_match', 'domains': []}
    return result

class Repeats:
    """Only suspicious promotion counts, scoped to a group. Never learns or bans."""
    def __init__(self): self.entries = {}

    def check(self, message, policy, result):
        if not features(policy)['repeat'] or result['level'] == 'clean': return None
        key = fingerprint(message.get('text') or message.get('caption') or '')
        if len(key) < 12: return None
        # Keep exact links in the digest; stripping punctuation must not conflate destinations.
        digest = hashlib.sha256((key + repr(sorted(extract_links(message)))).encode()).hexdigest()
        cid = message['chat']['id']; mid = message['message_id']; now = time.monotonic()
        self.entries = {k: v for k, v in self.entries.items() if now - v['ts'] < 120}
        bucket = self.entries.setdefault((cid, digest), {'ts': now, 'messages': set()})
        bucket['messages'].add(mid)
        if len(self.entries) > 500: self.entries.pop(next(iter(self.entries)))
        if len(bucket['messages']) >= 3:
            return {**result, 'level': 'suspected', 'reason': 'repeated_promotion',
                    'signals': ['本群120秒内至少3条相同推广内容']}
        return None
