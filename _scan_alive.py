# -*- coding: utf-8 -*-
"""只读扫:看哪些母号还活着。逐母号采样几个子号查积分(cookie-replay,不登录/不出图/不换号,每子号住宅IP)。
判定:子号有>=1000积分→母号活;子号有token但积分0/无权益→母号被封(org权益被收);子号cookie全失效→测不准。"""
import sys
import concurrent.futures as cf
import admin_console_manage as acm, console_children, network_proxy, _quota
import firefly_register_yescaptcha as fry

SAMPLE = int(sys.argv[1]) if len(sys.argv) > 1 else 3
cfg, cons = acm._load_consoles()
ckmap = {str(e.get("name") or "").lower(): (e.get("cookie") or "") for e in fry._load_adobe2api_cookie_entries()}


def check_one(em):
    ck = ckmap.get(em.lower(), "")
    if not ck:
        return em, "nock"
    pxy = network_proxy.proxy_for_id(em)
    px = {"http": pxy, "https": pxy} if pxy else None
    tok, _ = _quota._refresh_to_token(ck, px)
    if not tok:
        return em, "ckdead"
    try:
        q = _quota._fetch_credits(tok, px) or {}
    except Exception:
        return em, "err"
    tot = q.get("total")
    if isinstance(tot, int):
        return em, tot          # 数字:0=被收权益, >=1000=活
    return em, "noquota"        # 刷到token但查不出额度(403)=权益被收


tasks = []
for c in cons:
    sel = c.get("admin_email") or c.get("name") or ""
    kids = [k.get("email") for k in console_children.get_children(sel) if k.get("email")][:SAMPLE]
    tasks.append((sel, kids))
flat = [em for _sel, kids in tasks for em in kids]
print("扫 %d 母号 / 采样 %d 子号(只读查积分,每子号住宅IP)..." % (len(tasks), len(flat)), flush=True)

quota = {}
with cf.ThreadPoolExecutor(max_workers=8) as ex:
    for em, v in ex.map(check_one, flat):
        quota[em.lower()] = v

alive = dead = unknown = 0
dead_list = []
print("\n=== 母号存活 ===", flush=True)
for sel, kids in tasks:
    vals = [quota.get(k.lower()) for k in kids]
    good = [v for v in vals if isinstance(v, int) and v >= 1000]
    has_token = [v for v in vals if v not in ("nock", "ckdead", "err", None)]
    if good:
        alive += 1; st = "✅ 活 (子号 %d 分)" % good[0]
    elif has_token and all((v == 0 or v == "noquota") for v in has_token):
        dead += 1; dead_list.append(sel); st = "❌ 死/被封 (子号有token但0积分/无权益)"
    else:
        unknown += 1; st = "? 测不准 (子号cookie失效/无: %s)" % vals
    print("  %-40s %s" % (sel[:40], st), flush=True)

print("\n=== 汇总: 活 %d | 死/封 %d | 测不准 %d (共 %d 母号) ===" % (alive, dead, unknown, len(tasks)), flush=True)
if dead_list:
    print("\n死/封母号清单:", flush=True)
    for s in dead_list:
        print("  " + s, flush=True)
