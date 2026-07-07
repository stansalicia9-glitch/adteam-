# -*- coding: utf-8 -*-
"""探查 adobe2api 池结构(在生产机执行):tokens.json / refresh_profile.json / push_source_map.json,
并按25个死母号统计死子号数量、确认能按 email 在各文件里定位删除。只读,不改任何文件。"""
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

print('=== tokens.json', type(t).__name__, len(t), '条')
print('  [0] keys:', list(t[0].keys()))
print('  [0]非value:', {k: str(v)[:45] for k, v in t[0].items() if k != 'value'})
ef = [k for k in t[0].keys() if 'mail' in k.lower() or k in ('account', 'username', 'user', 'name')]
print('  tokens里email候选字段:', ef)

print('=== refresh_profile.json', type(r).__name__, len(r))
if isinstance(r, dict):
    k0 = list(r)[0]; v = r[k0]
    print('  key样本:', repr(k0)[:70])
    print('  val:', ('dict fields ' + str(list(v)[:12]) if isinstance(v, dict) else str(v)[:90]))

print('=== push_source_map', len(m), '映射; 不同母号', len(set(m.values())))
dead_subs = [s for s, mas in m.items() if mas in DEAD]
alive_subs = [s for s, mas in m.items() if mas not in DEAD]
print('  >>> 属于25死母号的死子号:', len(dead_subs))
print('  >>> 属于活母号的子号:', len(alive_subs))
print('  死子号样本:', dead_subs[:4])

# 死子号能否在各文件里按 email 定位
if isinstance(r, dict):
    rk = set(r.keys())
    print('  死子号在 refresh_profile(按email key)命中:', sum(1 for s in dead_subs if s in rk), '/', len(dead_subs))
# tokens 按候选字段匹配
if ef:
    f = ef[0]
    tokemails = {str(x.get(f, '')).lower() for x in t}
    print(f'  死子号在 tokens(按{f})命中:', sum(1 for s in dead_subs if s.lower() in tokemails), '/', len(dead_subs))
else:
    # tokens 没有email字段, 看 value(JWT) 里能否反查 —— 先看 status 分布
    print('  tokens 无email字段; status分布:', dict(Counter(str(x.get('status')) for x in t)))
