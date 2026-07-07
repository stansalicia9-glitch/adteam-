# -*- coding: utf-8 -*-
"""UMAPI 批量：列产品profile -> 列当前用户 -> 删非管理员 -> 并发安全预占加小号 -> 标记池。
纯 HTTP，不开浏览器。复用 admin_console_manage 的并发去重(锁内原子预占)。

config(admin_console_config.json) 每个 console 需:
  umapi_client_id / umapi_client_secret  (developer.adobe.com 的 S2S 项目)
  org_id(可省，自动从 product_users_url 提取) / product_profile(可省，自动猜 CC Pro)
  keep_admin_emails
"""
import argparse
import sys

import adobe_umapi as umapi
import admin_console_manage as acm


def _pick_profile(profiles, console):
    want = (console.get("product_profile") or "").strip().lower()
    if want:
        for p in profiles:
            if p["name"].lower() == want:
                return p["name"]
    for p in profiles:  # 猜 Creative Cloud Pro
        if "creative cloud pro" in (str(p.get("productName", "")) + str(p.get("name", ""))).lower():
            return p["name"]
    for p in profiles:  # 退而求其次：任一 PRODUCT 类型 profile
        if "PRODUCT" in str(p.get("type", "")).upper():
            return p["name"]
    return profiles[0]["name"] if profiles else None


def _creds(console):
    org_id = console.get("org_id") or umapi.org_id_from_url(console.get("product_users_url", ""))
    return org_id, console.get("umapi_client_id", ""), console.get("umapi_client_secret", "")


def process_console_umapi(console, add_file, target_seats, dry_run):
    tag = console.get("name") or console.get("admin_email") or "console"
    org_id, cid, secret = _creds(console)
    print("=" * 64, flush=True)
    if not (org_id and cid and secret):
        print(f"[{tag}] 缺 UMAPI 凭证(org_id/client_id/client_secret)，跳过", flush=True)
        return []
    print(f"[{tag}] UMAPI 处理 org={org_id}", flush=True)
    token = umapi.get_token(cid, secret)
    profiles = umapi.list_product_profiles(org_id, cid, token)
    profile = _pick_profile(profiles, console)
    print(f"[{tag}] product profiles: {[p['name'] for p in profiles]}", flush=True)
    print(f"[{tag}] 选用 profile: {profile!r}", flush=True)
    if not profile:
        print(f"[{tag}] 没找到 product profile，跳过", flush=True)
        return []

    keep = {e.lower() for e in console.get("keep_admin_emails", [])}
    current = umapi.list_group_users(org_id, cid, token, profile)
    to_remove = sorted(e for e in current if e not in keep)
    print(f"[{tag}] 当前 {len(current)} 用户；保留管理员 {sorted(keep)}；拟删 {len(to_remove)}: {to_remove}", flush=True)

    if dry_run:
        assigned = acm._assigned_emails()
        picks = [a for a in acm._load_add_accounts(add_file)
                 if a["email"].lower() not in assigned and a["email"].lower() not in current][:target_seats]
        print(f"[{tag}] [dry-run] 拟加 {len(picks)}: {[a['email'] for a in picks]}（未实际操作）", flush=True)
        return []

    # 实删非管理员
    if to_remove:
        umapi.remove_users(org_id, cid, token, to_remove, profile)
        print(f"[{tag}] ✅ 已删除 {len(to_remove)} 个非管理员", flush=True)

    # 原子预占（并发安全，绝不重号）+ 加（排除当前成员）
    picks = acm._reserve_accounts(add_file, target_seats, current, tag)
    emails = [a["email"] for a in picks]
    added = []
    if emails:
        try:
            results = umapi.add_users(org_id, cid, token, emails, profile)
            for r in results:
                print(f"[{tag}] add 结果: completed={r.get('completed')} notCompleted={r.get('notCompleted')} "
                      f"errors={(r.get('errors') or [])[:3]}", flush=True)
            added = emails  # v1：按批成功计；逐个失败可后续按 errors 精修
        except Exception as exc:
            print(f"[{tag}] 批量加失败: {exc}", flush=True)
    for acc in picks:
        if acc["email"] in added:
            acm.append_line_locked(acm.ADDED_FILE, acc["raw"])
            acm._mark_pool_success(acc, tag)
        else:
            acm._release_assigned(acc)
    print(f"[{tag}] ✅ 已添加 {len(added)} 个", flush=True)
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

    if args.test:
        for c in consoles:
            tag = c.get("name") or c.get("admin_email")
            org_id, cid, secret = _creds(c)
            try:
                res = umapi.test_connection(org_id, cid, secret)
                print(f"[{tag}] ✅ 连接OK org={org_id}", flush=True)
                for pr in res["profiles"]:
                    print(f"    - profile: {pr['name']!r} | product={pr.get('productName','')} | type={pr.get('type','')} | members={pr.get('memberCount','')}", flush=True)
            except Exception as exc:
                print(f"[{tag}] ❌ 连接失败: {exc}", flush=True)
        return 0

    total = 0
    for console in consoles[: args.limit] if args.limit else consoles:
        total += len(process_console_umapi(console, args.add_file, target_seats, args.dry_run))
    print("#" * 64, flush=True)
    print(f"完成。本次共添加 {total} 个 -> {acm.ADDED_FILE}", flush=True)
    if not args.dry_run and total and args.then_extract:
        import os, subprocess
        print("继续下半部分：登录->选企业->跳过手机号->导出cookie", flush=True)
        subprocess.call([sys.executable, os.path.join(acm.BASE_DIR, "firefly_login_extract_cookies.py"),
                         "--accounts", acm.ADDED_FILE, "--workers", str(args.workers), "--headless"])
    return 0


def parse_args(argv):
    ap = argparse.ArgumentParser(description="Adobe UMAPI 批量删/加成员（纯接口，不开浏览器）")
    ap.add_argument("--add-file", default=acm.REGISTERED_ACCOUNTS_FILE, help="要添加的小号文件")
    ap.add_argument("--seats", type=int, default=0, help="每台添加席位数")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--console", default="", help="按名字/邮箱只处理某管理员")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true", help="只打印拟删/拟加")
    ap.add_argument("--test", action="store_true", help="只测连接 + 列出 product profiles")
    ap.add_argument("--then-extract", action="store_true", help="加完直接导 cookie")
    import os
    args = ap.parse_args(argv)
    args.add_file = os.path.abspath(args.add_file)
    return args


if __name__ == "__main__":
    raise SystemExit(run(parse_args(sys.argv[1:])))
