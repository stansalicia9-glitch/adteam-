# -*- coding: utf-8 -*-
"""实测 55 个母号还活几个:ensure_token(验旧/cookie续/接码)→ JIL list_product_users。
   ALIVE=200(报子号数) / BANNED=403(企业权限被收) / NOPROD=404(产品没了) /
   LOGIN_FAIL=拿不到token(RT死|接码失败|被封无企业profile)。并发5(防批量登录触发风控)。"""
import os, sys, time, json
import concurrent.futures as cf
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import admin_console_manage as acm
import admin_jil_refresh_token as rtok
import adobe_jil as jil
import network_proxy

NOOP = lambda *a, **k: None
cfg, cons = acm._load_consoles()
N = len(cons)
done = [0]


def test_one(c):
    em = c.get("admin_email") or "?"
    org = c.get("org_id"); prod = c.get("product_id"); grp = str(c.get("group"))
    px = network_proxy.proxy_for_console(c)
    state, detail, seats = "?", "", None
    try:
        tok, _ref = rtok.ensure_token(c, px, log=NOOP)
    except Exception as e:
        tok = None; detail = "登录异常:" + str(e)[:40]
    if not tok:
        state = "LOGIN_FAIL"; detail = detail or "拿不到token(RT死/接码失败/被封无企业profile)"
    else:
        jil.set_console_proxy(px)
        try:
            users = jil.list_product_users(org, prod, tok)
            state = "ALIVE"; seats = len(users)
        except Exception as e:
            msg = str(e)
            if "403" in msg:
                state = "BANNED"; detail = "JIL 403 企业权限被收回"
            elif "404" in msg:
                state = "NOPROD"; detail = "JIL 404 产品没了"
            else:
                state = "JIL_ERR"; detail = msg[:50]
    done[0] += 1
    icon = {"ALIVE": "✅", "BANNED": "❌封", "NOPROD": "❌无产品", "LOGIN_FAIL": "⚠登录失败", "JIL_ERR": "⚠"}.get(state, "?")
    print("@@RES [%2d/%2d] %s %-40s grp=%-8s %s %s" % (
        done[0], N, icon, em[:40], grp, ("%d子号" % seats if seats is not None else ""), detail), flush=True)
    return (em, grp, state, seats, detail)


print("@@RES ==== 实测 %d 个母号(并发5)====" % N, flush=True)
results = []
with cf.ThreadPoolExecutor(max_workers=5) as ex:
    futs = [ex.submit(test_one, c) for c in cons]
    for f in cf.as_completed(futs):
        results.append(f.result())

# 汇总
cnt = Counter(r[2] for r in results)
alive = [r for r in results if r[2] == "ALIVE"]
banned = [r for r in results if r[2] == "BANNED"]
noprod = [r for r in results if r[2] == "NOPROD"]
loginfail = [r for r in results if r[2] == "LOGIN_FAIL"]
print("\n@@RES ================= 汇总 =================", flush=True)
print("@@RES 总数 %d  →  ✅活 %d | ❌封403 %d | ❌无产品404 %d | ⚠登录失败 %d | ⚠其它 %d" % (
    N, cnt.get("ALIVE", 0), cnt.get("BANNED", 0), cnt.get("NOPROD", 0),
    cnt.get("LOGIN_FAIL", 0), cnt.get("JIL_ERR", 0)), flush=True)
# 分组
bygrp = {}
for r in results:
    bygrp.setdefault(r[1], Counter())[r[2]] += 1
for g, c in bygrp.items():
    print("@@RES   分组 %-10s: 活%d 封%d 无产品%d 登录失败%d 其它%d" % (
        g, c.get("ALIVE", 0), c.get("BANNED", 0), c.get("NOPROD", 0), c.get("LOGIN_FAIL", 0), c.get("JIL_ERR", 0)), flush=True)
if alive:
    tot_seats = sum((r[3] or 0) for r in alive)
    print("@@RES   活母号合计子号位:%d 个(平均 %.1f/母号)" % (tot_seats, tot_seats / max(1, len(alive))), flush=True)
print("\n@@RES --- 活着的母号 ---", flush=True)
for r in alive:
    print("@@RES   ✅ %-40s %d子号" % (r[0][:40], r[3] or 0), flush=True)
print("@@RES --- 被封/掉权限的 ---", flush=True)
for r in banned + noprod:
    print("@@RES   ❌ %-40s %s" % (r[0][:40], r[4]), flush=True)
print("@@RES --- 登录失败(需人工复核,可能被封也可能RT死)---", flush=True)
for r in loginfail:
    print("@@RES   ⚠ %-40s %s" % (r[0][:40], r[4]), flush=True)

# 落盘一份结果
with open("_alive_result.json", "w", encoding="utf-8") as f:
    json.dump({"checked_at": int(time.time()), "summary": dict(cnt),
               "results": [{"email": r[0], "group": r[1], "state": r[2], "seats": r[3], "detail": r[4]} for r in results]},
              f, ensure_ascii=False, indent=2)
print("\n@@RES 结果已存 _alive_result.json", flush=True)
