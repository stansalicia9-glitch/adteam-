# -*- coding: utf-8 -*-
"""从 Adobe【真实成员】(JIL list_product_users)同步回本地子号清单(console_children)。
修复"🗑误删了假死真号 → 本地清单 < Adobe 真实":拉每个母号 Adobe 真实子号(排除管理员),
跟账号库(added/registered/team_seats)匹配账密拿完整 raw,写回 console_children。
★纯本地清单恢复:不碰 Adobe、不导cookie。匹配不到账密的成员会跳过(没密码导不了,会报数)。
用法: python _sync_children.py [--console X] [--dry-run] [--workers 3]
"""
import sys, re, os, argparse
import concurrent.futures as cf

import admin_console_manage as acm
import admin_jil_manage as ajm
import admin_jil_refresh_token as rt
import adobe_jil as jil
import console_children
import network_proxy

BASE = os.path.dirname(os.path.abspath(__file__))
ACCOUNT_FILES = ("added_accounts.txt", "registered_accounts.txt", "team_seats.txt")


def build_raw_index():
    """email_lower → 原始账密行(raw)。"""
    idx = {}
    for fn in ACCOUNT_FILES:
        p = os.path.join(BASE, fn)
        try:
            for l in open(p, encoding="utf-8").read().splitlines():
                l = l.strip()
                if not l:
                    continue
                segs = re.split(r"----|\s+", l)
                if not segs or "@" not in segs[0]:
                    continue
                idx.setdefault(segs[0].lower(), l)  # 首次出现的原始行当 raw
        except Exception:
            pass
    return idx


def sync_one(console, raw_idx, proxy, dry_run):
    tag = console.get("name") or console.get("admin_email")
    tok, _ = rt.ensure_token(console, proxy)
    if not tok:
        print(f"[{tag}] ❌ 无token,跳过", flush=True)
        return {"console": tag, "ok": False, "msg": "无token"}
    org_id, product_id, token = ajm._ids(console)
    keep = {e.lower() for e in (console.get("keep_admin_emails") or [])}
    keep.add(str(console.get("admin_email") or "").strip().lower())
    try:
        users = jil.list_product_users(org_id, product_id, token)
    except Exception as e:
        print(f"[{tag}] ❌ JIL异常 {str(e)[:50]}", flush=True)
        return {"console": tag, "ok": False, "msg": "JIL异常"}
    subs = [u["email"] for u in users if u["email"].lower() not in keep]

    cur = console_children.get_children(console)
    cur_emails = {str(r.get("email") or "").lower() for r in cur}
    cur_raw = {str(r.get("email") or "").lower(): r.get("raw") for r in cur}

    rows, miss = [], 0
    for em in subs:
        raw = raw_idx.get(em.lower()) or cur_raw.get(em.lower()) or ""
        if raw:
            rows.append({"email": em, "raw": raw})
        else:
            miss += 1  # 账号库+现有清单都没账密 → 导不了,跳过
    added_back = len([r for r in rows if r["email"].lower() not in cur_emails])
    print(f"[{tag}] Adobe真实子号 {len(subs)} | 写回 {len(rows)} (补回 {added_back}) | 无账密跳过 {miss}", flush=True)
    if not dry_run and rows:
        console_children.set_children(console, rows)
    return {"console": tag, "ok": True, "real": len(subs), "written": len(rows),
            "added_back": added_back, "miss": miss}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--console", default="", help="只同步指定母号")
    ap.add_argument("--dry-run", action="store_true", help="只看会补回多少、不写")
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()

    cfg, consoles = acm._load_consoles()
    proxy = network_proxy.configured_proxy() or (cfg.get("proxy") or "").strip() or None
    if a.console:
        k = a.console.strip().lower()
        consoles = [c for c in consoles if k in str(c.get("admin_email", "")).lower() or k in str(c.get("name", "")).lower()]
    if not consoles:
        print("没有匹配的母号")
        return 1
    raw_idx = build_raw_index()
    print(f"==== 从 Adobe 同步清单:{len(consoles)} 母号,账号库 {len(raw_idx)} 条,"
          f"{'只看(不写)' if a.dry_run else '写回本地清单'}(并发{a.workers}) ====", flush=True)
    with cf.ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        res = list(ex.map(lambda c: sync_one(c, raw_idx, proxy, a.dry_run), consoles))

    oks = [r for r in res if r.get("ok")]
    print("\n==== 汇总 ====", flush=True)
    print(f"母号 {len(res)} | Adobe真实子号合计 {sum(r.get('real',0) for r in oks)} | "
          f"写回清单合计 {sum(r.get('written',0) for r in oks)} | 补回 {sum(r.get('added_back',0) for r in oks)} | "
          f"无账密 {sum(r.get('miss',0) for r in oks)}", flush=True)
    for r in res:
        if not r.get("ok"):
            print(f"  {r['console']} ❌ {r.get('msg')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
