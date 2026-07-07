# -*- coding: utf-8 -*-
"""清掉 org 归零的死号:从三次诊断 output 解析死号(只删确认 org归零 的,限流/待测/活的保留),
备份 admin_console_config.json 后从 consoles 移除死号条目。"""
import json, os, re, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, 'admin_console_config.json')
TASK = r"C:\Users\Administrator\AppData\Local\Temp\claude\C--Users-Administrator\75d55933-aeda-479f-9a51-546e8a467234\tasks"
OUTS = ['b4wyz8l3j.output', 'b0u0o2sq2.output', 'btaksakpu.output']  # 直连全量 + 代理重测9 + 代理重测2

line_re = re.compile(r'idx=\s*(-?\d+)\s+orgs=\s*(-?\d+)\s+subs=\s*(-?\d+)\s+(\S+)\s+\|\s+(\S+@\S+)')
status = {}  # email -> 最新result(后面文件覆盖前面,即代理重测结果优先)
for o in OUTS:
    p = os.path.join(TASK, o)
    if not os.path.exists(p):
        print(f"[警告] 找不到 {p},跳过", flush=True); continue
    for line in open(p, encoding='utf-8'):
        m = line_re.search(line)
        if m:
            status[m.group(5).strip().lower()] = m.group(4)

dead = sorted(e for e, r in status.items() if 'org归零' in r)
alive = sorted(e for e, r in status.items() if r == '活')
limited = sorted(e for e, r in status.items() if ('限流' in r or '失败' in r))
print(f"解析诊断: ✅活{len(alive)}  ❌死{len(dead)}  ⏳限流/待测{len(limited)}", flush=True)
print("将删除的死号(org归零):", flush=True)
for e in dead:
    print("   -", e, flush=True)
print("保留的限流/待测号(不删):", limited, flush=True)

bak = CFG + '.bak_before_clean_dead'
if not os.path.exists(bak):
    shutil.copy2(CFG, bak); print(f"\n已备份原 config → {os.path.basename(bak)}", flush=True)
else:
    print(f"\n备份已存在(保留最早原始,不覆盖): {os.path.basename(bak)}", flush=True)

d = json.load(open(CFG, encoding='utf-8'))
cs = d['consoles']
dead_set = set(dead)
before = len(cs)
kept = [c for c in cs if (c.get('admin_email') or c.get('email') or '').strip().lower() not in dead_set]
removed = [c for c in cs if (c.get('admin_email') or c.get('email') or '').strip().lower() in dead_set]

# 安全校验:删的必须都在死号集合里
bad = [c.get('admin_email') for c in removed if (c.get('admin_email') or '').strip().lower() not in dead_set]
if bad:
    print("⚠️ 异常,中止(误删非死号):", bad, flush=True); raise SystemExit(1)

d['consoles'] = kept
json.dump(d, open(CFG, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print(f"\n✅ 清理完成: 原 {before} 个母号 → 删 {len(removed)} 死号 → 剩 {len(kept)} 个", flush=True)
kept_emails = [(c.get('admin_email') or '').lower() for c in kept]
still_alive = [e for e in kept_emails if e in set(alive)]
still_limited = [e for e in kept_emails if e in set(limited)]
print(f"   剩余构成: ✅活 {len(still_alive)} + ⏳限流待测 {len(still_limited)} + 未诊断 {len(kept)-len(still_alive)-len(still_limited)}", flush=True)
print(f"   误删的死号备份在 {os.path.basename(bak)},要恢复随时说", flush=True)
