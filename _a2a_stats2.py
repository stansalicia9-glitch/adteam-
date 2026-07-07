# -*- coding: utf-8 -*-
"""统计 adobe2api 池的【客观死号信号】:fails/error_until/credits=0,以及和25死母号的交叉。只读。"""
import json, os, time
from collections import Counter

CFG = '/app/config' if os.path.exists('/app/config/tokens.json') else '/opt/adobe2api/source/config'
DEAD = {e + '@hotmail.com' for e in [
    'metzgerxilftrteel', 'gipsontolentinoamsz3', 'paisleypikedcjou', 'briannakairxqya', 'luisnevaehnoust',
    'riverz9ffbzparent', 'levi19njtrdexter', 'eliasmcmullenyrdygt', 'vangbundyucx85', 'creelamorykacfy',
    'journeysmartsccvmw', 'montelongojt4yawoaks', 'cerdabolin9r3b0z', 'laylaabdulbfbqj', 'bontrager5sv00butts',
    'milatfdijbindira', 'loganreahe5kb0', 'delossantosghr7wlaria', 'elam29y5sptoliver', 'gaelaureliaqzjwnq',
    'jenkins45bahpalmer', 'johngaelcqgmd', 'ripleyjansenrafxgi', 'hyltonrrqdvstringer', 'kensley7h4e4mehta']}

t = json.load(open(CFG + '/tokens.json'))
m = json.load(open(CFG + '/push_source_map.json'))
now = time.time()


def f(x, k):
    try:
        return float(x.get(k) or 0)
    except Exception:
        return 0.0


print('总token:', len(t))
print('fails 分布:', dict(Counter(int(f(x, 'fails')) for x in t).most_common(10)))
print('error_until 在未来(被禁用):', sum(1 for x in t if f(x, 'error_until') > now))
print('credits=0:', sum(1 for x in t if int(f(x, 'credits_available')) == 0))
print('fails>=3:', sum(1 for x in t if f(x, 'fails') >= 3), '| fails>=5:', sum(1 for x in t if f(x, 'fails') >= 5),
      '| fails>=10:', sum(1 for x in t if f(x, 'fails') >= 10))

# 客观死号候选:credits=0 或 fails>=5 或 被禁
def is_dead(x):
    return int(f(x, 'credits_available')) == 0 or f(x, 'fails') >= 5 or f(x, 'error_until') > now

cand = [x for x in t if is_dead(x)]
print('\n>>> 客观死号候选(credits=0 或 fails>=5 或 error_until未来):', len(cand), '/', len(t))
print('   plan:', dict(Counter(x.get('free_quota_plan') for x in cand).most_common()))
print('   域:', dict(Counter((x.get('refresh_profile_email') or '').split('@')[-1] for x in cand).most_common(5)))

# 更严格:credits=0 且 fails>=3 (基本确定废)
strict = [x for x in t if int(f(x, 'credits_available')) == 0 and f(x, 'fails') >= 3]
print('>>> 严格死号(credits=0 且 fails>=3):', len(strict))

# 25死母号关联(push_source_map子号 email 匹配 refresh_profile_email)
ds = {s.lower() for s, mas in m.items() if mas in DEAD}
dt = [x for x in t if (x.get('refresh_profile_email') or '').lower() in ds]
print('\n25死母号的子号在池(email匹配):', len(dt))
print('   其中客观已死(我们的is_dead判定):', sum(1 for x in dt if is_dead(x)))
print('   其中还在工作(credits>=1且fails<5):', sum(1 for x in dt if not is_dead(x)))
