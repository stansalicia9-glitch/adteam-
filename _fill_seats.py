# -*- coding: utf-8 -*-
"""检测每个母号【真实席位】(Adobe JIL list_product_users),不满 target(默认9)就从邮箱池【补差额】
——★只加不删、保留现有所有子号(含已售客户号),只补缺口;补的新号协议导cookie写本地池(激活4000)。
跟 admin_jil_manage 的"删加子号"(process_console_jil 删光重建)本质不同:这里绝不删任何现有号。
用法:
  python _fill_seats.py --dry-run            # 只检测每个母号缺几个,不补
  python _fill_seats.py [--seats 9] [--console X|--only a,b] [--workers 3] [--no-extract]
"""
import sys, re, argparse
import concurrent.futures as cf

import admin_console_manage as acm
import admin_jil_manage as ajm
import admin_jil_refresh_token as rt
import adobe_jil as jil
import console_children
import admin_login_protocol as alp
import _quota
import network_proxy
import firefly_register_yescaptcha as fry


def _parse(raw):
    segs = re.split(r"----|\s+", str(raw or "").strip())
    email = (segs[0] if segs else "").strip()
    pw = segs[1].strip() if len(segs) > 1 else ""
    rt_ = next((s for s in segs[2:] if s.startswith("M.")), "")
    cid = next((s for s in segs[2:] if len(s) == 36 and s.count("-") == 4), "")
    return {"email": email, "password": pw, "refresh_token": rt_, "client_id": cid}


def fill_one(console, target, proxy, dry_run, do_extract):
    tag = console.get("name") or console.get("admin_email") or "console"
    tok, _ = rt.ensure_token(console, proxy)
    if not tok:
        print(f"[{tag}] ❌ 无可用token,跳过", flush=True)
        return {"console": tag, "ok": False, "msg": "无token"}
    org_id, product_id, token = ajm._ids(console)
    try:
        groups = jil.get_license_groups(org_id, product_id, token)
        lg = ajm._pick_lg(groups, console)
    except Exception as e:
        print(f"[{tag}] ❌ JIL异常 {str(e)[:60]}", flush=True)
        return {"console": tag, "ok": False, "msg": "JIL异常"}
    if not lg:
        return {"console": tag, "ok": False, "msg": "无license group"}

    keep = {e.lower() for e in (console.get("keep_admin_emails") or [])}
    keep.add(str(console.get("admin_email") or "").strip().lower())   # ★管理员自己不占子号席位
    users = jil.list_product_users(org_id, product_id, token)   # [{email,id}](含管理员)
    subs = [u for u in users if u["email"].lower() not in keep]
    cur = len(subs)
    current_emails = {u["email"].lower() for u in users}        # 占号去重用【全部】(含管理员,免重复加)
    seats = target if target > 0 else (int(console.get("seats") or 0) or 9)
    gap = max(0, seats - cur)
    print(f"[{tag}] 真实席位 {cur}/{seats} → 缺 {gap} 个", flush=True)
    if gap <= 0:
        return {"console": tag, "ok": True, "current": cur, "seats": seats, "gap": 0, "added": 0, "cookied": 0}
    if dry_run:
        return {"console": tag, "ok": True, "current": cur, "seats": seats, "gap": gap, "added": 0, "cookied": 0, "dry": True}

    # ★从邮箱池预占 gap 个(跨进程文件锁,绝不重号),只加不删
    picks = ajm._reserve_for_team(ajm.MAIL_POOL_SOURCE, gap, current_emails, tag)
    emails = [a["email"] for a in picks]
    added = []
    if emails:
        try:
            results = jil.add_users(org_id, product_id, lg, token, emails)
            for r in results:
                print(f"[{tag}] add status={r['status']}", flush=True)
            if results and all(r["status"] in (200, 201) for r in results):
                added = emails
            elif results and all(r["status"] in (200, 201, 207) for r in results):
                # 207 部分成功:以【重列实际成员】为准,别把批里失败的邮箱也算 added
                try:
                    now = {u["email"].lower() for u in jil.list_product_users(org_id, product_id, token)}
                    added = [e for e in emails if e.lower() in now]
                    print(f"[{tag}] 207 部分成功:实际加进 {len(added)}/{len(emails)}", flush=True)
                except Exception as _ve:
                    print(f"[{tag}] 207 复核失败({str(_ve)[:50]}),保守按全部已提交计", flush=True)
                    added = emails
        except Exception as e:
            print(f"[{tag}] 加号失败 {str(e)[:60]}", flush=True)
    for acc in picks:
        if acc["email"] in added:
            acm.append_line_locked(acm.ADDED_FILE, acc["raw"])
            acm._mark_pool_success(acc, tag)
        else:
            ajm._release_reserved(acc, ajm.MAIL_POOL_SOURCE)
    # ★追加到 console_children(保留现有、不覆盖;set_children 内部按 email 去重)
    if added:
        existing = console_children.get_children(console)
        new_rows = [{"email": acc["email"], "raw": acc["raw"]} for acc in picks if acc["email"] in added]
        console_children.set_children(console, list(existing) + new_rows)

    # 协议导新号 cookie 写本地池(激活拿4000)
    cookied = 0
    if added and do_extract:
        ok4000 = []
        for acc in picks:
            if acc["email"] not in added:
                continue
            a = _parse(acc["raw"])
            try:
                ck = alp.sub_login_cookie(a, proxy=proxy)
            except Exception as e:
                ck = ""
                print(f"[{tag}] 补号导cookie异常 {a['email']} {str(e)[:50]}", flush=True)
            if ck:
                tot = 0
                try:
                    tot = _quota.query_quota(ck, proxy).get("total") or 0
                except Exception:
                    pass
                ok4000.append({"email": a["email"], "cookie": ck})
                cookied += 1
                print(f"[{tag}] 补号导cookie {a['email']} 积分总/{tot}", flush=True)
        if ok4000:
            _bn = {str(e.get("name") or "").lower(): e for e in fry._load_adobe2api_cookie_entries()}
            for a in ok4000:
                _bn[a["email"].lower()] = {"name": a["email"], "cookie": a["cookie"]}
            fry._write_adobe2api_cookie_entries(list(_bn.values()))
    return {"console": tag, "ok": True, "current": cur, "seats": seats, "gap": gap, "added": len(added), "cookied": cookied}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seats", type=int, default=0, help="目标席位(0=用config target_seats_per_console或母号seats,默认9)")
    ap.add_argument("--console", default="", help="只跑指定母号")
    ap.add_argument("--only", default="", help="逗号分隔母号,只跑这些")
    ap.add_argument("--dry-run", action="store_true", help="只检测每个母号缺几个、不补")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--no-extract", action="store_true", help="只加号、不导cookie")
    a = ap.parse_args()

    cfg, consoles = acm._load_consoles()
    target = a.seats if a.seats > 0 else int(cfg.get("target_seats_per_console", 9))
    proxy = network_proxy.configured_proxy() or (cfg.get("proxy") or "").strip() or None
    if a.console:
        k = a.console.strip().lower()
        consoles = [c for c in consoles if k in str(c.get("admin_email", "")).lower() or k in str(c.get("name", "")).lower()]
    elif a.only:
        want = {e.strip().lower() for e in a.only.split(",") if e.strip()}
        consoles = [c for c in consoles if str(c.get("admin_email", "")).strip().lower() in want
                    or str(c.get("name", "")).strip().lower() in want]
    if not consoles:
        print("没有匹配的母号")
        return 1

    print(f"==== 检测 {len(consoles)} 个母号真实席位(目标{target}),{'只看缺口' if a.dry_run else '补差额'}"
          f"(并发{a.workers},{'不导cookie' if a.no_extract else '补完导cookie'}) ====", flush=True)
    do_extract = not a.no_extract
    with cf.ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        res = list(ex.map(lambda c: fill_one(c, target, proxy, a.dry_run, do_extract), consoles))

    notfull = [r for r in res if r.get("ok") and r.get("gap")]
    print("\n==== 汇总 ====", flush=True)
    print(f"母号 {len(res)} | 不满的 {len(notfull)} | 缺口合计 {sum(r.get('gap', 0) for r in notfull)}", flush=True)
    if not a.dry_run:
        print(f"本次补加 {sum(r.get('added', 0) for r in res)} | 导cookie {sum(r.get('cookied', 0) for r in res)}", flush=True)
    for r in sorted(res, key=lambda x: -(x.get("gap") or 0)):
        if r.get("ok"):
            line = f"  {r['console']}: {r.get('current')}/{r.get('seats')} 缺{r.get('gap', 0)}"
            if not a.dry_run and r.get("gap"):
                line += f" → 补{r.get('added', 0)} 导ck{r.get('cookied', 0)}"
            print(line, flush=True)
        else:
            print(f"  {r['console']}: ❌ {r.get('msg')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
