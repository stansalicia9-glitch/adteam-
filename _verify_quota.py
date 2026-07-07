# -*- coding: utf-8 -*-
"""摸底母号自己(owner)的 Firefly 团队额度:母号走 Firefly 上下文导cookie查积分。
母号>100=org额度还在(子号值得修/导);母号≤10=org Firefly额度已耗尽/到期(废)。
用法: python _verify_quota.py            # 全量25母号(并发)
      python _verify_quota.py howell ..  # 指定关键字"""
import sys
import concurrent.futures as cf
import admin_console_manage as acm
import admin_login_protocol as alp
import _quota

cfg, cs = acm._load_consoles()
arg = [a for a in sys.argv[1:] if a != "--all"]
targets = cs if not arg else [c for c in cs if any(k.lower() in (c.get("admin_email") or "").lower() for k in arg)]
WORKERS = 5


def _one(c):
    em = c["admin_email"]
    acc = {
        "email": em,
        "password": c.get("admin_password") or c.get("admin_password_alt") or "",
        "refresh_token": c.get("admin_refresh_token") or "",
        "client_id": c.get("admin_client_id") or "",
    }
    try:
        ck = alp.sub_login_cookie(acc, proxy=None)
    except Exception as e:
        print(f"[{em}] ❌异常 {str(e)[:45]}", flush=True)
        return em, -1
    if not ck:
        print(f"[{em}] ❌登录失败/限流", flush=True)
        return em, -1
    try:
        q = _quota.query_quota(ck)
        tot = q.get("total") or 0
        av = q.get("available")
    except Exception:
        tot, av = -1, None
    flag = "✅额度在" if tot > 100 else ("❌额度没了" if tot >= 0 else "登录失败")
    print(f"[{em}] 母号Firefly {av}/{tot} {flag}", flush=True)
    return em, tot


print(f"==== 全量摸底 {len(targets)} 母号自己 Firefly 额度(并发{WORKERS},直连) ====", flush=True)
res = []
with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
    res = list(ex.map(_one, targets))

alive = [e for e, t in res if t > 100]
dead = [e for e, t in res if 0 <= t <= 100]
fail = [e for e, t in res if t < 0]
print(f"\n==== 汇总 总{len(res)} | ✅额度在(母号>100) {len(alive)} | ❌额度没了(母号≤10) {len(dead)} | 登录失败/限流 {len(fail)} ====", flush=True)
if alive:
    print("✅ 额度还在的母号(子号值得修/导4000):", flush=True)
    for e in alive:
        print("  ", e, flush=True)
if dead:
    print("❌ 额度没了的母号(org废,子号永远10):", flush=True)
    for e in dead:
        print("  ", e, flush=True)
