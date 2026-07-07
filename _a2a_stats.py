# -*- coding: utf-8 -*-
"""统计 adobe2api 池健康度:积分分布/plan/status/域,以及死母号子号的匹配方式。只读。"""
import json, os
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
r = json.load(open(CFG + '/refresh_profile.json'))


def ca(x):
    try:
        return int(float(x.get('credits_available') or 0))
    except Exception:
        return -1


print('refresh_profile.json keys:', list(r.keys()))
for k in r:
    if k != 'version':
        sub = r[k]
        print('  ', k, type(sub).__name__, len(sub) if hasattr(sub, '__len__') else '')
        if isinstance(sub, dict):
            k0 = list(sub)[0]
            print('     子key样本:', repr(k0)[:60], '| 值fields:', list(sub[k0])[:10] if isinstance(sub[k0], dict) else type(sub[k0]).__name__)

print('\n总token:', len(t))
print('credits_available分桶:', dict(Counter(('=0' if ca(x) == 0 else '1-99' if ca(x) < 100 else '100-3999' if ca(x) < 4000 else '4000+') for x in t)))
print('free_quota_plan:', dict(Counter(x.get('free_quota_plan') for x in t).most_common()))
print('status:', dict(Counter(x.get('status') for x in t).most_common()))
print('email域:', dict(Counter((x.get('refresh_profile_email') or '').split('@')[-1] for x in t).most_common(8)))

dead_subs = {s.lower() for s, mas in m.items() if mas in DEAD}
print('\n死母号子号(push_source_map):', len(dead_subs))
dead_in_tok = [x for x in t if (x.get('refresh_profile_email') or '').lower() in dead_subs]
print('死子号在tokens(refresh_profile_email匹配):', len(dead_in_tok),
      '积分:', dict(Counter(('=0' if ca(x) == 0 else '<100' if ca(x) < 100 else '100+') for x in dead_in_tok)))
adp = [x for x in t if 'adpuhao' in (x.get('refresh_profile_email') or '')]
print('tokens里adpuhao域号:', len(adp), '| 这些积分:', dict(Counter(('=0' if ca(x) == 0 else '<100' if ca(x) < 100 else '100+') for x in adp)))

# 低积分废号总量(不分来源)
low = [x for x in t if 0 <= ca(x) < 100]
print('\n全池 credits<100 的废号:', len(low), '/', len(t))
print('  其中plan分布:', dict(Counter(x.get('free_quota_plan') for x in low).most_common()))
