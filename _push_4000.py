# -*- coding: utf-8 -*-
"""把 console_children 里所有子号纯协议高并发导出 cookie,只把积分≥阈值(真团队4000档)的推 adobe2api。
一次导、不等传播重试(普号10直接跳过)。推送走 cookie_push(直连生产),导cookie/查积分走 config.proxy。
用法: python _push_4000.py [--workers 8] [--threshold 1000] [--push] [--console <母号>]
"""
import sys, re, argparse, time
import concurrent.futures as cf

import admin_console_manage as acm
import console_children
import admin_login_protocol as alp
import _quota
import network_proxy


def parse_acc(raw):
    segs = re.split(r"----|\s+", str(raw or "").strip())
    email = (segs[0] if segs else "").strip()
    pw = segs[1].strip() if len(segs) > 1 else ""
    rt = next((s for s in segs[2:] if s.startswith("M.")), "")
    cid = next((s for s in segs[2:] if len(s) == 36 and s.count("-") == 4), "")
    return {"email": email, "password": pw, "refresh_token": rt, "client_id": cid}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8, help="导出并发(默认8)")
    ap.add_argument("--threshold", type=int, default=1000, help="积分(total)≥此值才推(真团队≈4000;普号10被滤掉)")
    ap.add_argument("--push", action="store_true", help="把达标的推 adobe2api")
    ap.add_argument("--console", default="", help="只跑指定母号(默认全部25个)")
    ap.add_argument("--skip-pooled", action="store_true", help="跳过已在本地池且有cookie的号(补跑/续跑用,大幅提速)")
    args = ap.parse_args()

    proxy = network_proxy.configured_proxy() or None
    cfg, consoles = acm._load_consoles()
    if args.console:
        key = args.console.strip().lower()
        consoles = [c for c in consoles if (c.get("admin_email") or "").lower() == key or (c.get("name") or "").lower() == key]

    pooled = set()
    if args.skip_pooled:
        try:
            import firefly_register_yescaptcha as _fry
            pooled = {str(e.get("name") or "").strip().lower()
                      for e in _fry._load_adobe2api_cookie_entries() if (e.get("cookie") or "")}
        except Exception:
            pooled = set()

    tasks = []  # (母号sel, account)
    skipped = 0
    for c in consoles:
        sel = c.get("admin_email") or c.get("name")
        for ch in (console_children.get_children(c) or []):
            acc = parse_acc(ch.get("raw"))
            if acc["email"] and acc["password"]:
                if args.skip_pooled and acc["email"].strip().lower() in pooled:
                    skipped += 1
                    continue
                tasks.append((sel, acc))
    if args.skip_pooled:
        print(f"[--skip-pooled] 跳过已在本地池 {skipped} 个,只跑剩 {len(tasks)} 个", flush=True)

    print(f"==== 共 {len(tasks)} 个子号 / {len(consoles)} 母号,纯协议高并发导出(并发{args.workers},走{'代理' if proxy else '直连'}),阈值≥{args.threshold} ====", flush=True)
    if not tasks:
        print("没有子号"); return 1

    done = [0]
    n = len(tasks)

    def _one(item):
        sel, acc = item
        em = acc["email"]
        try:
            ck = alp.sub_login_cookie(acc, proxy=proxy)
        except Exception as e:
            done[0] += 1
            print(f"[{done[0]}/{n}] {em} ❌导出异常 {str(e)[:50]}", flush=True)
            return sel, em, "", 0
        if not ck:
            done[0] += 1
            print(f"[{done[0]}/{n}] {em} ❌登录失败/无cookie", flush=True)
            return sel, em, "", 0
        tot, av = 0, None
        try:
            q = _quota.query_quota(ck); tot = q.get("total") or 0; av = q.get("available")
        except Exception:
            pass
        done[0] += 1
        flag = "✅团队档·推" if tot >= args.threshold else f"普号{tot}·跳过"
        print(f"[{done[0]}/{n}] {em} cookie{len(ck)}字 积分{av}/{tot} {flag}", flush=True)
        return sel, em, ck, tot

    # ★按母号【边导边推】:每个母号子号导完(并发)立刻推 adobe2api,实时看池从0往上涨
    import cookie_push
    pcfg = cookie_push.config_for_group("")
    by_sel = {}
    for sel, acc in tasks:
        by_sel.setdefault(sel, []).append(acc)

    total_ok = total_push = total_login = 0
    for ci, (sel, accs) in enumerate(by_sel.items(), 1):
        with cf.ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            res = list(ex.map(lambda a, _s=sel: _one((_s, a)), accs))
        ok4000 = [{"email": em, "cookie": ck} for (s, em, ck, tot) in res if ck and tot >= args.threshold]
        total_login += sum(1 for r in res if r[2])
        total_ok += len(ok4000)
        if ok4000:  # ★写本地 cookie 池(firefly_adobe2api_cookies.json),供 _autoswap 监控这批子号
            try:
                import firefly_register_yescaptcha as _fry
                _bn = {str(e.get("name") or "").lower(): e for e in _fry._load_adobe2api_cookie_entries()}
                for a in ok4000:
                    _bn[a["email"].lower()] = {"name": a["email"], "cookie": a["cookie"]}
                _fry._write_adobe2api_cookie_entries(list(_bn.values()))
            except Exception as _we:
                print("[写本地池] 异常: %s" % str(_we)[:60], flush=True)
        if args.push and ok4000:
            rec = {}
            for _ in range(3):
                rec = cookie_push._push_now(sel, ok4000, cfg=pcfg, force=True)
                if rec.get("status") in ("accepted", "partial"):
                    break
                time.sleep(3)
            sc = rec.get("sent_count") or 0
            total_push += sc
            print(f"#### [{ci}/{len(by_sel)}母号] {sel}: 4000档 {len(ok4000)}/{len(accs)} → 推 {rec.get('status')} 入池 {sc} | 累计推 {total_push} ####", flush=True)
        else:
            print(f"#### [{ci}/{len(by_sel)}母号] {sel}: 4000档 {len(ok4000)}/{len(accs)}(无可推/未带--push) ####", flush=True)

    print(f"\n==== ✅ 全部完成:登录成功 {total_login}/{n} | 4000档 {total_ok} | 推 adobe2api 共 {total_push} ====", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
