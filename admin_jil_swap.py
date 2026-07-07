# -*- coding: utf-8 -*-
"""换号:从某母号团队【删掉指定旧子号】+【从邮箱池加等量新子号】(JIL,不开浏览器),
可选 --then-extract:换完导出新子号CK + 逐母号推adobe。
★破坏性:真删 Adobe 团队成员。务必先 --dry-run 看清"拟删/拟加"再实跑。
旧子号被踢出团队后自动失去 firefly 权益→adobe2api 那边自然变无额度死号,不需额外删。

复用 admin_jil_manage 的 JIL 原语。用法:
  python admin_jil_swap.py --console <母号邮箱> --old a@x.com,b@x.com [--dry-run] [--then-extract]
  python admin_jil_swap.py --swaps-file swaps.json [--dry-run] [--then-extract]
      swaps.json = [{"console":"m@x.com","old":["a@x.com","b@x.com"]}, ...]
"""
import argparse
import io
import json
import os
import random
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import admin_jil_manage as ajm

jil = ajm.jil
rt = ajm.rt
acm = ajm.acm
pool = ajm.pool
console_children = ajm.console_children


def _find_console(consoles, sel):
    s = str(sel or "").strip().lower()
    for c in consoles:
        if s == str(c.get("admin_email", "")).strip().lower() or s == str(c.get("name", "")).strip().lower():
            return c
    return None


def swap_one_console(console, old_emails, proxy, dry_run, pre_token=None):
    """对一个母号:删掉 old_emails 里的子号、加等量新子号。返回新加的 account dict 列表。
    pre_token=(tok,did):run() 阶段1已并发预登录拿到的 token,跳过这里的 ensure_token(母号级并发加速点)。"""
    # ★每母号专属住宅 IP:pre_token 时会跳过 ensure_token(它会 set),这里补一次,保证 JIL 走该母号固定住宅出口
    try:
        import network_proxy as _np
        jil.set_console_proxy(_np.proxy_for_console(console))
    except Exception:
        pass
    tag = console.get("name") or console.get("admin_email") or "console"
    old_set = {str(e).strip().lower() for e in (old_emails or []) if str(e).strip()}
    print("=" * 64, flush=True)
    print(f"[{tag}] 换号:请求删 {len(old_set)} 个旧子号 {sorted(old_set)}", flush=True)
    if not old_set:
        print(f"[{tag}] 没指定旧子号,跳过", flush=True)
        return []

    if pre_token is not None:
        tok, did = pre_token  # 已并发预登录,直接用
    else:
        tok, did = rt.ensure_token(console, proxy)
    if not tok:
        print(f"[{tag}] ❌ 无可用 token(先登录母号/播种),跳过", flush=True)
        return []
    if did:
        try:
            acm.save_consoles_merge([console])
        except Exception:
            pass

    org_id, product_id, token = ajm._ids(console)
    try:
        groups = jil.get_license_groups(org_id, product_id, token)
    except Exception as exc:
        print(f"[{tag}] ❌ 取 license-groups 失败({str(exc)[:90]});多半是协议登录拿到 personal token"
              f"(母号没选到企业admin profile)或token失效/bps-il超时 → 跳过该母号(不崩,其它母号继续)", flush=True)
        return []
    lg = ajm._pick_lg(groups, console)
    if not lg:
        print(f"[{tag}] 没找到 license group,跳过", flush=True)
        return []

    keep = {e.lower() for e in console.get("keep_admin_emails", [])}
    users = jil.list_product_users(org_id, product_id, token)  # [{email,id}]
    current_emails = {u["email"].lower() for u in users}
    to_remove = [u for u in users
                 if u["email"].lower() in old_set and u["email"].lower() not in keep and u.get("id")]
    found = {u["email"].lower() for u in to_remove}
    missing = old_set - found
    if missing:
        print(f"[{tag}] ⚠️ 这些旧子号不在当前团队/或是管理员,跳过:{sorted(missing)}", flush=True)
    want = len(to_remove)  # 删几个加几个
    print(f"[{tag}] 实际能删 {want}: {[u['email'] for u in to_remove]}", flush=True)
    if want == 0:
        print(f"[{tag}] 没有可删的旧子号,跳过", flush=True)
        return []

    if dry_run:
        data = pool.list_accounts(limit=1000000)
        picks = [item["email"] for item in data.get("items", [])
                 if item.get("status") == "available" and item["email"].lower() not in current_emails][:want]
        print(f"[{tag}] [DRY-RUN] 拟删 {[u['email'] for u in to_remove]}", flush=True)
        print(f"[{tag}] [DRY-RUN] 拟从邮箱池加 {want} 个: {picks}（未实际操作）", flush=True)
        return []

    # 1) 删旧
    try:
        rr = jil.remove_users(org_id, product_id, lg, token, [u["id"] for u in to_remove])
        ok_rm = all(r["status"] in (200, 204, 207) for r in rr)
        print(f"[{tag}] 删旧 status={[r['status'] for r in rr]} {'✅' if ok_rm else '⚠️部分失败'}", flush=True)
    except Exception as exc:
        print(f"[{tag}] 删旧失败:{exc},中止该母号(不加新,免席位错乱)", flush=True)
        return []

    # ★删→加之间拟人延迟:别一秒踢一秒加(原子踢加=机器特征),停 ~15s 再加,像真人管理员
    if not dry_run:
        _d = random.uniform(13, 17)
        print(f"[{tag}] 删旧完成,停 {_d:.0f}s 再加新(拟人,避免原子踢加)…", flush=True)
        time.sleep(_d)

    current_after = current_emails - found
    # 2) 加等量新
    picks = ajm._reserve_for_team(ajm.MAIL_POOL_SOURCE, want, current_after, tag)
    emails = [a["email"] for a in picks]
    added = []
    if emails:
        try:
            results = jil.add_users(org_id, product_id, lg, token, emails)
            print(f"[{tag}] 加新 status={[r['status'] for r in results]}", flush=True)
            if all(r["status"] in (200, 201, 207) for r in results):
                added = emails
        except Exception as exc:
            print(f"[{tag}] 加新失败:{exc}", flush=True)
    added_accounts = []
    for acc in picks:
        if acc["email"] in added:
            acm.append_line_locked(acm.ADDED_FILE, acc["raw"])
            acm._mark_pool_success(acc, tag)
            added_accounts.append(acc)
        else:
            ajm._release_reserved(acc, ajm.MAIL_POOL_SOURCE)

    # 3) 更新 console_children:去掉旧的 + 加新的(其它子号保留)
    cur = console_children.get_children(console) or []
    new_children = [c for c in cur if str(c.get("email", "")).lower() not in found]
    new_children += added_accounts
    console_children.set_children(console, new_children)
    print(f"[{tag}] ✅ 换号完成:删 {want} / 加 {len(added_accounts)};新子号={[a['email'] for a in added_accounts]}", flush=True)
    return added_accounts


def run(args):
    cfg, consoles = acm._load_consoles()
    proxy = (cfg.get("proxy") or "").strip() or None
    if args.swaps_file:
        swaps = json.load(open(args.swaps_file, encoding="utf-8"))
    elif args.console:
        swaps = [{"console": args.console, "old": [e for e in (args.old or "").split(",") if e.strip()]}]
    else:
        raise SystemExit("需要 --console + --old,或 --swaps-file")

    # 解析母号
    tasks = []
    for sw in swaps:
        c = _find_console(consoles, sw.get("console"))
        if not c:
            print(f"没找到母号 {sw.get('console')}", flush=True)
            continue
        tasks.append((c, sw.get("old") or []))

    # ★阶段1:并发预登录母号(ensure_token 协议拿码最慢,这是母号级并发的加速点)
    #   throttle(限速错峰,全自动监控用):母号【串行】预登录,别短时间同时登录一堆母号
    throttle = bool(getattr(args, "throttle", False))
    # ★母号级并发(用户选档:5)。每母号走各自住宅IP/不同org,几个母号并行相对安全;
    #   提速靠"母号并行",防封靠"每母号内部删旧停5s再加新"(见 swap_one_console)+ 每母号独立IP。
    cw = max(5, int(getattr(args, "console_workers", 3) or 3))

    def _pre(item):
        c, old = item
        ctag = c.get("admin_email") or c.get("name") or "console"
        try:
            tok, did = rt.ensure_token(c, proxy)
        except Exception as exc:
            print(f"[{ctag}] 预登录异常,跳过: {str(exc)[:90]}", flush=True)
            tok, did = None, False
        return c, old, tok, did

    if cw > 1 and len(tasks) > 1:
        import concurrent.futures as _cf
        print(f"#### 阶段1:并发预登录 {len(tasks)} 个母号(并发={cw}) ####", flush=True)
        with _cf.ThreadPoolExecutor(max_workers=cw) as ex:
            pre = list(ex.map(_pre, tasks))
    else:
        pre = [_pre(t) for t in tasks]

    # 阶段2:换号(删旧+加新),母号级并发=cw(各走各自住宅IP/不同org;adobe_jil 用 threading.local 存代理,
    #   并发各走各IP、线程安全)。每母号内部仍"删旧停5s再加新"防原子踢加;去掉了母号间大停顿,靠并发提速。
    all_new = []
    swapped = []  # [(console, [新账号dict]) ...]

    def _do_swap(item):
        c, old, tok, did = item
        ctag = c.get("admin_email") or c.get("name") or "console"
        try:
            new = swap_one_console(c, old, proxy, args.dry_run, pre_token=(tok, did))
        except Exception as exc:
            print(f"[{ctag}] 换号异常,跳过(其它母号继续): {str(exc)[:100]}", flush=True)
            return None
        return (c, new) if new else None

    if cw > 1 and len(pre) > 1:
        import concurrent.futures as _cf2
        print(f"#### 阶段2:并发换号 {len(pre)} 个母号(并发={cw}) ####", flush=True)
        with _cf2.ThreadPoolExecutor(max_workers=cw) as ex:
            _results2 = list(ex.map(_do_swap, pre))
    else:
        _results2 = [_do_swap(item) for item in pre]
    for _r in _results2:
        if _r:
            all_new += _r[1]
            swapped.append(_r)

    print("#" * 64, flush=True)
    print(f"换号汇总:本次新加 {len(all_new)} 个子号", flush=True)

    if not args.dry_run and all_new and args.then_extract:
        import time as _t
        import re as _re
        import concurrent.futures
        import admin_login_protocol as alp
        import _quota
        delay = max(0, int(getattr(args, "export_delay", 180)))
        if delay:
            # ★新号刚加进团队 firefly 权益没传播到位时,Firefly 上下文 filtered_profiles 还没企业profile
            #   → 只能拿到 personal(普号10分废cookie);隔几分钟权益传播后才有企业框→4000。
            #   等 delay 让它传播;下面每号还带"普号→等60s重试"兜底;就算全没传播也不丢号,随时再【协议全流程导入】补。
            print(f"#### 等 {delay}s 让新号 firefly 团队权益传播(没传播会导到普号10分,带重试兜底) ####", flush=True)
            _t.sleep(delay)

        def _acc_of(a):
            raw = str(a.get("raw") or "")
            segs = _re.split(r"----|\s+", raw.strip())
            pw = segs[1] if len(segs) > 1 else ""
            rt = next((s for s in segs[2:] if s.startswith("M.")), "")
            cid = next((s for s in segs[2:] if len(s) == 36 and s.count("-") == 4), "")
            return {"email": (a.get("email") or "").strip(), "password": pw, "refresh_token": rt, "client_id": cid}

        print(f"#### 协议导出新子号 cookie({len(all_new)} 个,纯 HTTP 零浏览器) ####", flush=True)
        cookie_results = {}

        def _exp(a):
            acc = _acc_of(a)
            if not acc["password"]:
                print(f"  [协议导出] {acc['email']} → ❌raw 没解析出密码", flush=True)
                return acc["email"], ""
            import network_proxy as _np
            _pxy = _np.proxy_for_id(acc["email"])  # ★每子号专属住宅IP:别用全局proxy(空=直连Adobe、暴露本机IP)
            ck = ""
            for _r in range(2):  # 普号(权益没传播)再等60s重试一次
                ck = alp.sub_login_cookie(acc, proxy=_pxy)
                if not ck:
                    print(f"  [协议导出] {acc['email']} → ❌登录失败", flush=True)
                    break
                q = _quota.query_quota(ck)
                tot = q.get("total") or 0
                print(f"  [协议导出] {acc['email']} → cookie {len(ck)}字 积分{q.get('available')}/{tot}", flush=True)
                if tot > 100:
                    break
                print(f"  [协议导出] {acc['email']} 还是普号({tot}分=权益没传播),等60s重试", flush=True)
                _t.sleep(60)
            return acc["email"], ck

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            for em, ck in ex.map(_exp, all_new):
                cookie_results[em.lower()] = ck

        # ★维护本地 cookie 池(firefly_adobe2api_cookies.json,供 _autoswap 监控):删掉换走的旧号 + 写入新号4000 cookie
        try:
            import firefly_register_yescaptcha as _fry
            _bn = {str(e.get("name") or "").lower(): e for e in _fry._load_adobe2api_cookie_entries()}
            _del = 0
            for sw in swaps:
                for _old in (sw.get("old") or []):
                    if _bn.pop(str(_old).strip().lower(), None) is not None:
                        _del += 1
            _allnew = [{"email": (a.get("email") or "").strip(), "cookie": cookie_results.get((a.get("email") or "").strip().lower())}
                       for _c, _na in swapped for a in _na if cookie_results.get((a.get("email") or "").strip().lower())]
            for a in _allnew:
                _bn[a["email"].lower()] = {"name": a["email"], "cookie": a["cookie"]}
            _fry._write_adobe2api_cookie_entries(list(_bn.values()))
            print(f"#### 本地cookie池已更新:删旧号 {_del} + 写新号 {len(_allnew)} ####", flush=True)
            # ★换号删旧号 → 同步从"已售"清单(exported_accounts.txt)清掉旧号:旧号回收、该号位变回【未售】,新号可重新卖
            try:
                import _export_a2a
                _olds = set()
                for sw in swaps:
                    for _old in (sw.get("old") or []):
                        _olds.add(str(_old).strip().lower())
                _cur = _export_a2a.load_exported()
                _b4 = len(_cur)
                _cur -= _olds
                if len(_cur) != _b4:
                    with open(_export_a2a.EXPORTED_FILE, "w", encoding="utf-8") as _ef:
                        for _e in sorted(_cur):
                            _ef.write(_e + "\n")
                    print(f"#### 已售清单:换掉的旧号清出已售 {_b4 - len(_cur)} 个(这些号位已变回未售、可再卖) ####", flush=True)
            except Exception as _xe:
                print(f"#### 清理已售清单异常:{_xe} ####", flush=True)
        except Exception as _we:
            print(f"#### 维护本地cookie池异常:{_we} ####", flush=True)

        # ★只推【新换的子号】,不重推整个母号——否则没换的老号会被重复导入 adobe2api(产生重复)
        if getattr(args, "no_push", False):
            print("#### --no-push:换号只导cookie进本地池、不推adobe(推送交给导出门禁那套统一把关) ####", flush=True)
            return 0
        try:
            import cookie_push
            pcfg = cookie_push.config_for_group("")
            for c, new_accs in swapped:
                sel = c.get("admin_email") or c.get("name")
                accts = [{"email": (a.get("email") or "").strip(), "cookie": cookie_results.get((a.get("email") or "").strip().lower())}
                         for a in new_accs if cookie_results.get((a.get("email") or "").strip().lower())]
                if not accts:
                    print(f"#### {sel}: 新号都没导出成功(没cookie),没东西推 ####", flush=True)
                    continue
                rec = None
                for _pt in range(3):   # 推送 transient 失败重试(实测偶发 0/N,重推就成)
                    rec = cookie_push._push_now(sel, accts, cfg=pcfg, force=True)
                    if rec.get("status") in ("accepted", "partial"):
                        break
                    _t.sleep(3)
                print(f"#### 推送 {sel} 只推新号 -> {rec.get('status')} 收到/入池 {rec.get('sent_count')}/{len(accts)} ####", flush=True)
        except Exception as exc:
            print(f"#### 推送阶段异常:{exc} ####", flush=True)
    return 0


def parse_args(argv):
    ap = argparse.ArgumentParser(description="换号:删指定旧子号 + 加等量新子号(JIL),可选导出+推adobe")
    ap.add_argument("--console", default="", help="母号邮箱/名称")
    ap.add_argument("--old", default="", help="逗号分隔的旧子号邮箱(要换掉的)")
    ap.add_argument("--swaps-file", default="", help="JSON: [{console, old:[...]}, ...]")
    ap.add_argument("--dry-run", action="store_true", help="只看拟删/拟加,不实际操作(强烈建议先跑)")
    ap.add_argument("--then-extract", action="store_true", help="换完导出新子号CK + 逐母号推adobe")
    ap.add_argument("--no-push", action="store_true", help="配合--then-extract:只导cookie进本地池、不推adobe(推送交给导出门禁)")
    ap.add_argument("--export-delay", type=int, default=180, help="加新号后等N秒再导出(等firefly权益传播,默认180;真正兜底是3轮重跑,短了也丢不了号)")
    ap.add_argument("--workers", type=int, default=3, help="子号导出CK的并发")
    ap.add_argument("--console-workers", type=int, default=3, help="母号级并发:阶段1同时预登录几个母号(默认3,换号提速)")
    ap.add_argument("--throttle", action="store_true", help="限速错峰(全自动监控用):母号串行预登录+母号间30~90s随机间隔+删加间隔15s,拟人节奏防风控")
    return ap.parse_args(argv)


if __name__ == "__main__":
    sys.exit(run(parse_args(sys.argv[1:])))
