# -*- coding: utf-8 -*-
"""完整全流程协议(母号 → adobe2api 导入,全程纯 HTTP、零浏览器):
① 母号协议登录拿 jil_token → ② JIL 列真团队子号 → ③ 账号库匹配密码 →
④ 子号协议登录 Firefly 导出 cookie(并发) → ⑤(可选)查积分验证 → ⑥(可选)推 adobe2api。
用法: python full_flow_protocol.py <母号email或index> [--limit N] [--workers N] [--check-credits] [--push]
"""
import argparse
import concurrent.futures
import json
import re
import sys

import admin_login_protocol as alp
import adobe_jil as jil
import _quota


def build_account_index():
    """账号库 email → {email,password,[refresh_token,client_id]}(added/registered)。"""
    idx = {}
    for fn in ("added_accounts.txt", "registered_accounts.txt"):
        try:
            for l in open(fn, encoding="utf-8").read().splitlines():
                if not l.strip():
                    continue
                segs = re.split(r"----|\s+", l.strip())
                if len(segs) < 2 or "@" not in segs[0]:
                    continue
                rec = {"email": segs[0], "password": segs[1]}
                for s in segs[2:]:
                    if s.startswith("M."):
                        rec["refresh_token"] = s
                    elif len(s) == 36 and s.count("-") == 4:
                        rec["client_id"] = s
                idx.setdefault(segs[0].lower(), rec)
        except Exception:
            pass
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("console", help="母号 email 或 consoles 索引(数字)")
    ap.add_argument("--limit", type=int, default=2, help="只导前 N 个子号(实测用)")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--check-credits", action="store_true", help="导出后查 Firefly 积分(验证真团队号 4000)")
    ap.add_argument("--push", action="store_true", help="推 adobe2api")
    args = ap.parse_args()

    cfg = json.load(open("admin_console_config.json", encoding="utf-8-sig"))
    if args.console.isdigit():
        c = cfg["consoles"][int(args.console)]
    else:
        c = [x for x in cfg["consoles"] if x["admin_email"] == args.console][0]
    cemail = c["admin_email"]

    print("① 母号协议登录:", cemail, flush=True)
    tok = alp.protocol_login(c)
    if not tok:
        print("❌ 母号协议登录失败")
        return 1
    org = c["org_id"]
    prod = c["product_id"]
    print("   ✅ jil_token %d字, org=%s" % (len(tok), org), flush=True)

    users = jil.list_product_users(org, prod, tok)
    subs = [u["email"] for u in users if u["email"].lower() != cemail.lower()]
    print("② JIL 真团队子号 %d个" % len(subs), flush=True)

    idx = build_account_index()
    accounts = []
    for e in subs:
        rec = idx.get(e.lower())
        if rec:
            accounts.append(rec)
    accounts = accounts[:args.limit]
    print("③ 账号库有密码可导的 %d个(本次限 %d):" % (len([e for e in subs if e.lower() in idx]), args.limit),
          [a["email"] for a in accounts], flush=True)

    def exp(a):
        ck = alp.sub_login_cookie(a)
        cr = ""
        if ck and args.check_credits:
            q = _quota.query_quota(ck)
            cr = "| 积分 %s/%s %s" % (q.get("available"), q.get("total"), q.get("reason"))
        print("  [导出] %s → %s %s" % (a["email"], ("cookie %d字" % len(ck)) if ck else "❌失败", cr), flush=True)
        return {"email": a["email"], "cookie": ck or ""}

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(exp, accounts))
    ok = [r for r in results if r["cookie"]]
    print("④ 协议导出成功 %d/%d" % (len(ok), len(accounts)), flush=True)

    if args.push and ok:
        import cookie_push
        pcfg = cookie_push.config_for_group("")
        rec = cookie_push._push_now(cemail, ok, cfg=pcfg, force=True)
        print("⑤ 推 adobe2api → %s, 收到/入池 %s/%d" % (rec.get("status"), rec.get("sent_count"), len(ok)), flush=True)
    print("=" * 50, flush=True)
    print("全流程完成。零浏览器、纯协议。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
