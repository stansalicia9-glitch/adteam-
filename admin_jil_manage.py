# -*- coding: utf-8 -*-
"""JIL 批量加微软号到团队(纯接口，用管理员会话 token)。复用并发安全预占去重。
config 每个 console 需:
  jil_token   (从浏览器 Copy-as-cURL 抓的 Authorization Bearer，约24h过期，过期重抓)
  product_users_url (自动提取 org_id + product_id)
  keep_admin_emails
加号来源默认 team_seats.txt(每行一个微软邮箱，或 邮箱----密码... 格式)。
注意：团队席位要加【微软号 outlook/hotmail】，@pengfeiapi.xyz 会被 Adobe 拒。
"""
import argparse
import sys
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

import adobe_jil as jil
import admin_console_manage as acm
import admin_jil_refresh_token as rt
import console_children
import firefly_mail_pool as pool

TEAM_SEATS_FILE = os.path.join(acm.BASE_DIR, "team_seats.txt")
MAIL_POOL_SOURCE = "__mail_pool__"


def _ids(console):
    url = console.get("product_users_url", "")
    org_id = console.get("org_id") or jil.org_id_from_url(url)
    product_id = console.get("product_id") or jil.product_id_from_url(url)
    return org_id, product_id, console.get("jil_token", "")


def _pick_lg(groups, console):
    want = str(console.get("license_group_id") or "").strip()
    if want:
        return want
    return groups[0]["id"] if groups else None


def _pool_record_to_account(record):
    raw = record.get("raw") or "----".join([
        record.get("email") or "",
        record.get("password") or "",
        record.get("email_password") or "",
        record.get("client_id") or pool.DEFAULT_CLIENT_ID,
        record.get("refresh_token") or "",
    ])
    return {
        "email": record.get("email") or "",
        "password": record.get("password") or "",
        "email_password": record.get("email_password") or "",
        "raw": raw,
    }


def _reserve_for_team(add_file, want, current_emails, tag):
    if add_file == MAIL_POOL_SOURCE:
        records = pool.acquire_accounts(want, exclude_emails=current_emails, reason=f"reserved for team {tag}")
        picks = [_pool_record_to_account(r) for r in records]
        picks = [p for p in picks if p.get("email")]
        if picks:
            print(f"[{tag}] 已从邮箱池预占 {len(picks)} 个小号: {[a['email'] for a in picks]}", flush=True)
        return picks
    return acm._reserve_accounts(add_file, want, current_emails, tag)


def _release_reserved(acc, add_file):
    if add_file == MAIL_POOL_SOURCE:
        pool.mark_account(acc["email"], "available", "team add failed, released")
    else:
        acm._release_assigned(acc)


def _write_extract_accounts(rows):
    fd, path = tempfile.mkstemp(prefix="jil_current_children_", suffix=".txt", dir=acm.BASE_DIR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for row in rows or []:
            raw = str((row or {}).get("raw") or "").strip()
            if raw:
                f.write(raw + "\n")
    return path


def process_console_jil(console, add_file, target_seats, dry_run):
    tag = console.get("name") or console.get("admin_email") or "console"
    org_id, product_id, token = _ids(console)
    print("=" * 64, flush=True)
    if not (org_id and product_id and token):
        print(f"[{tag}] 缺 org_id/product_id/jil_token，跳过", flush=True)
        return []
    print(f"[{tag}] JIL org={org_id} product={product_id}", flush=True)
    groups = jil.get_license_groups(org_id, product_id, token)
    lg = _pick_lg(groups, console)
    print(f"[{tag}] license groups: {groups}", flush=True)
    print(f"[{tag}] 选用 license group: {lg}", flush=True)
    if not lg:
        print(f"[{tag}] 没找到 license group，跳过", flush=True)
        return []

    keep = {e.lower() for e in console.get("keep_admin_emails", [])}
    # ★基于【组织全体用户】算要删的(不只产品用户)——这样历史遗留的"无产品幽灵"也一并清掉,
    #   否则只看产品用户会漏删幽灵、越积越多。current_emails 同样用 org 全体(加号去重更准)。
    users = jil.list_org_users(org_id, token)   # [{email,id}] 含幽灵
    current_emails = {u["email"].lower() for u in users}
    to_remove = [u for u in users if u["email"].lower() not in keep and u.get("id")]
    print(f"[{tag}] 组织当前 {len(users)} 用户；保留管理员 {sorted(keep)}；拟删 {len(to_remove)}: {[u['email'] for u in to_remove]}", flush=True)

    if dry_run:
        if add_file == MAIL_POOL_SOURCE:
            data = pool.list_accounts(limit=1000000)
            picks = [
                {"email": item["email"]}
                for item in data.get("items", [])
                if item.get("status") == "available" and item["email"].lower() not in current_emails
            ][:target_seats]
        else:
            assigned = acm._assigned_emails()
            picks = [a for a in acm._load_add_accounts(add_file)
                     if a["email"].lower() not in assigned and a["email"].lower() not in current_emails][:target_seats]
        print(f"[{tag}] [dry-run] 拟加 {len(picks)}: {[a['email'] for a in picks]}（未实际操作）", flush=True)
        return []

    # 实删非管理员(保留管理员)
    if to_remove:
        try:
            rr = jil.remove_users(org_id, product_id, lg, token, [u["id"] for u in to_remove])
            for r in rr:
                print(f"[{tag}] remove status={r['status']}", flush=True)
            if all(r["status"] in (200, 204, 207) for r in rr):
                print(f"[{tag}] ✅ 已删除 {len(to_remove)} 个非管理员", flush=True)
                current_emails = set(keep)
        except Exception as exc:
            print(f"[{tag}] 删除失败: {exc}", flush=True)

    picks = _reserve_for_team(add_file, target_seats, current_emails, tag)
    emails = [a["email"] for a in picks]
    added = []
    if emails:
        try:
            results = jil.add_users(org_id, product_id, lg, token, emails)
            for r in results:
                print(f"[{tag}] add status={r['status']} -> {str(r['body'])[:300]}", flush=True)
                # 207 多状态：默认整批视为已提交；逐个失败可后续按 body 精修
            if all(r["status"] in (200, 201, 207) for r in results):
                added = emails
        except Exception as exc:
            print(f"[{tag}] 批量加失败: {exc}", flush=True)
    for acc in picks:
        if acc["email"] in added:
            acm.append_line_locked(acm.ADDED_FILE, acc["raw"])
            acm._mark_pool_success(acc, tag)
        else:
            _release_reserved(acc, add_file)
    console_children.set_children(console, [acc for acc in picks if acc["email"] in added])
    print(f"[{tag}] ✅ 已提交添加 {len(added)} 个", flush=True)
    return added


def run(args):
    cfg, consoles = acm._load_consoles()
    if not consoles:
        raise RuntimeError("admin_console_config.json 里没有 consoles")
    target_seats = args.seats if args.seats > 0 else int(cfg.get("target_seats_per_console", 9))
    if args.console:
        sel = args.console.strip().lower()
        consoles = [c for c in consoles if sel in str(c.get("name", "")).lower() or sel in str(c.get("admin_email", "")).lower()]
        if not consoles:
            raise RuntimeError(f"没找到匹配 --console={args.console} 的管理员")
    elif getattr(args, "only", ""):
        wanted = {e.strip().lower() for e in str(args.only).split(",") if e.strip()}
        consoles = [c for c in consoles
                    if str(c.get("admin_email", "")).strip().lower() in wanted
                    or str(c.get("name", "")).strip().lower() in wanted]
        if not consoles:
            raise RuntimeError("没找到勾选的母号")
        print(f"#### 只处理勾选的 {len(consoles)} 个母号 ####", flush=True)

    proxy = (cfg.get("proxy") or "").strip() or None
    workers = max(1, int(getattr(args, "workers", 1) or 1))

    if args.test:
        for c in consoles:
            tok, _ = rt.ensure_token(c, proxy)
            tag = c.get("name") or c.get("admin_email")
            if not tok:
                print(f"[{tag}] ❌ 无可用 token（先登录母号/播种）", flush=True)
                continue
            org_id, product_id, token = _ids(c)
            try:
                res = jil.test_token(org_id, product_id, token)
                print(f"[{tag}] ✅ token 有效 org={org_id} product={product_id}", flush=True)
                for g in res["license_groups"]:
                    print(f"    - license group: id={g['id']} name={g.get('name','')!r}", flush=True)
            except Exception as exc:
                print(f"[{tag}] ❌ 失败: {exc}", flush=True)
        acm.save_consoles_merge(consoles)
        return 0

    # 并发处理：每个母号 ensure_token + 删加（取号有跨进程文件锁，绝不重号）；完一个补一个直到全部完成
    refreshed_list = []

    def work(console):
        tok, did = rt.ensure_token(console, proxy)
        if did:
            refreshed_list.append(console)
        if not tok:
            print(f"[{console.get('name') or console.get('admin_email')}] 跳过（token 无法自动刷新）", flush=True)
            return []
        seats = args.seats if args.seats > 0 else (int(console.get("seats") or 0) or target_seats)
        return process_console_jil(console, args.add_file, seats, args.dry_run)

    target_consoles = consoles[: args.limit] if args.limit else consoles
    if workers <= 1 or len(target_consoles) <= 1:
        results = [work(c) for c in target_consoles]
    else:
        print(f"#### 并发处理 {len(target_consoles)} 个母号，workers={workers}（完一个补一个，直到全部完成）####", flush=True)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(work, target_consoles))
    if refreshed_list:
        acm.save_consoles_merge(refreshed_list)  # 加锁合并刷新到的 token

    total = sum(len(r) for r in results)
    print("#" * 64, flush=True)
    print(f"完成。本次共添加 {total} 个 -> {acm.ADDED_FILE}", flush=True)
    if not args.dry_run and total and args.then_extract:
        import subprocess
        extract_items = console_children.all_children() if len(target_consoles) != 1 else console_children.get_children(target_consoles[0])
        accounts_file = _write_extract_accounts(extract_items)
        cmd = [sys.executable, os.path.join(acm.BASE_DIR, "firefly_login_extract_cookies.py"),
               "--accounts", accounts_file, "--workers", str(args.workers), "--headless"]
        if proxy:
            cmd += ["--proxy", proxy]
        subprocess.call(cmd)
    return 0


def parse_args(argv):
    ap = argparse.ArgumentParser(description="Adobe JIL 批量加微软号到团队（纯接口，用会话token）")
    ap.add_argument("--add-file", default=MAIL_POOL_SOURCE, help="微软号来源，默认邮箱池；传文件路径则从文件取号")
    ap.add_argument("--seats", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--console", default="")
    ap.add_argument("--only", default="", help="逗号分隔的管理员邮箱/名称，只处理这些（勾选批量用）")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true", help="只读验证 token + 列 license groups")
    ap.add_argument("--then-extract", action="store_true")
    args = ap.parse_args(argv)
    if args.add_file != MAIL_POOL_SOURCE:
        args.add_file = os.path.abspath(args.add_file)
    return args


if __name__ == "__main__":
    raise SystemExit(run(parse_args(sys.argv[1:])))
