# -*- coding: utf-8 -*-
"""批量诊断所有母号 org 状态:每号独立子进程(日志隔离)+ 并发,收集 SUMMARY,汇总活/死/限流。
用法: python _diag_all_masters.py [并发数,默认4] [起始idx] [结束idx]
"""
import sys, json, os, subprocess
import concurrent.futures as cf

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(HERE, 'python', 'python.exe')
d = json.load(open(os.path.join(HERE, 'admin_console_config.json'), encoding='utf-8'))
cs = d if isinstance(d, list) else d.get('consoles') or d.get('admin_consoles') or []
N = len(cs)

workers = int(sys.argv[1]) if len(sys.argv) > 1 else 4
if len(sys.argv) > 2 and ',' in sys.argv[2]:
    idxs = [int(x) for x in sys.argv[2].split(',') if x.strip() != '']
else:
    i0 = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    i1 = int(sys.argv[3]) if len(sys.argv) > 3 else N
    idxs = list(range(i0, min(i1, N)))
print(f"==== 批量诊断 {len(idxs)} 个母号 idxs={idxs} 并发={workers} 代理={'有' if os.environ.get('PROXY') else '无(直连)'} ====", flush=True)


def parse(line):
    out = {}
    for kv in line.split('\t')[1:]:
        if '=' in kv:
            k, v = kv.split('=', 1); out[k] = v
    return out


def run_one(i):
    env = dict(os.environ); env['PYTHONIOENCODING'] = 'utf-8'
    try:
        p = subprocess.run([PY, '_diag_master.py', str(i)], cwd=HERE, capture_output=True,
                           text=True, timeout=220, env=env, encoding='utf-8', errors='replace')
        for line in reversed((p.stdout or '').splitlines()):
            if line.startswith('SUMMARY'):
                return line
        return f"SUMMARY\tidx={i}\temail=?\torgs=-1\tsubs=-1\tresult=无SUMMARY(崩/无输出)"
    except subprocess.TimeoutExpired:
        return f"SUMMARY\tidx={i}\temail=?\torgs=-1\tsubs=-1\tresult=超时220s"
    except Exception as e:
        return f"SUMMARY\tidx={i}\temail=?\torgs=-1\tsubs=-1\tresult=子进程异常:{str(e)[:40]}"


rows = []
done = 0
with cf.ThreadPoolExecutor(max_workers=workers) as ex:
    futs = {ex.submit(run_one, i): i for i in idxs}
    for f in cf.as_completed(futs):
        line = f.result(); rows.append(line); done += 1
        d2 = parse(line)
        print(f"[{done}/{len(idxs)}] idx={str(d2.get('idx')):>2} orgs={str(d2.get('orgs','?')):>3} "
              f"subs={str(d2.get('subs','?')):>5} {d2.get('result','?'):<24} | {d2.get('email','')}", flush=True)

ps = [parse(r) for r in rows]
ps.sort(key=lambda x: int(x.get('idx', 0)))
alive = [p for p in ps if p.get('result') == '活']
dead = [p for p in ps if 'org归零' in str(p.get('result'))]
limited = [p for p in ps if any(s in str(p.get('result')) for s in ('限流', '失败', '超时', '异常', '无SUMMARY'))]
other = [p for p in ps if p not in alive and p not in dead and p not in limited]
print(f"\n==== 汇总 总{len(ps)} | ✅活{len(alive)} | ❌org归零{len(dead)} | ⏳限流/拿码失败{len(limited)} | ❓其它{len(other)} ====", flush=True)
if alive:
    print("✅ 还活的母号(换号仍可用):", flush=True)
    for p in alive:
        print(f"   idx={p['idx']} subs={p.get('subs')} {p.get('email')}", flush=True)
if other:
    print("❓ 其它(org在但product403等,需细看):", flush=True)
    for p in other:
        print(f"   idx={p['idx']} {p.get('result')} {p.get('email')}", flush=True)
json.dump(ps, open(os.path.join(HERE, '_diag_all_result.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print(f"\n结果已存 _diag_all_result.json (限流/拿码失败的可单独 python _diag_master.py <idx> 重测)", flush=True)
