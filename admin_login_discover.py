# -*- coding: utf-8 -*-
"""登录母号 + 自动提取该有的 JSON：org_id / product_id / product_users_url / jil_token。
母号只需配 admin_email + admin_password（+ admin_refresh_token 用于邮箱验证码）。
流程：登录 adminconsole（已播种则复用 session）→ 从 URL/请求拿 org_id → 拦 bps-il 拿 token →
      列产品挑 Creative Cloud → 拼 users 地址 → 写回 config。之后扫描/删加子号即可。
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from playwright.sync_api import sync_playwright

import adobe_jil as jil
import admin_console_manage as acm

ROOT = "https://adminconsole.adobe.com/"
_DBG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_extract_debug.log")


def _dbg(tag, msg):
    try:
        with open(_DBG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} [{tag}] {msg}\n")
    except Exception:
        pass


def _save_cfg(cfg):
    with open(acm.CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def _org_from_url(url):
    m = re.search(r"([0-9A-Za-z]+@AdobeOrg)", str(url or ""))
    return m.group(1) if m else ""


def _pick_cc_pro(prods):
    """从产品列表挑 Creative Cloud（全部应用/Pro）。挑不到名字就退而取唯一/第一个。"""
    if not prods:
        return None
    for kw in ("creative cloud pro", "creative cloud all", "creative cloud", "全部应用",
               "all apps", "pro for teams", "creative"):
        for x in prods:
            if kw in str(x.get("name", "")).lower():
                return x
    return prods[0]


def _fill_product_and_seats(console, org, tok, tag):
    """已拿到 org+token → 列产品挑 Creative Cloud + 拼 users 地址 + 算席位,写入 console。返回 bool。
    协议登录 和 浏览器登录 共用。"""
    console["org_id"] = org
    console["jil_token"] = tok
    pick_total = None
    if not console.get("product_users_url"):
        try:
            prods = jil.list_products(org, tok)
        except Exception as exc:
            print(f"[{tag}] ❌ 列产品失败: {exc}", flush=True)
            return False
        pick = _pick_cc_pro(prods)
        if not pick:
            print(f"[{tag}] ⚠️ 没识别出 Creative Cloud 产品：{[(x.get('id'), x.get('name')) for x in prods]}", flush=True)
            return False
        console["product_id"] = pick["id"]
        console["product_users_url"] = f"{ROOT}{org}/products/{pick['id']}/users"
        pick_total = pick.get("total")
        print(f"[{tag}] 识别产品: {pick.get('name')!r} id={pick['id']} 总席位={pick_total or '?'}", flush=True)
    else:
        console["product_id"] = console.get("product_id") or jil.product_id_from_url(console["product_users_url"])
    if pick_total:
        total_seats = pick_total
    else:
        try:
            total_seats = jil.get_product_seats(org, console["product_id"], tok)
        except Exception:
            total_seats = 0
    admins = max(1, len([e for e in (console.get("keep_admin_emails") or [console.get("admin_email")]) if e]))
    if total_seats > 0:
        console["seats"] = max(0, total_seats - admins)
        print(f"[{tag}] 席位: 总 {total_seats} − 管理员 {admins} = 子号目标 {console['seats']}", flush=True)
    else:
        print(f"[{tag}] ⚠️ 没扫到总席位（产品字段名不匹配），请在表格手填【席位】", flush=True)
    print(f"[{tag}] ✅ 提取完成 org={org} product={console.get('product_id')} 席位={console.get('seats','?')}", flush=True)
    print(f"[{tag}]    product_users_url={console['product_users_url']}", flush=True)
    print(f"[{tag}]    jil_token={tok[:20]}...", flush=True)
    return True


def discover_one_protocol(console, proxy):
    """纯协议发现 org/product/token(不开浏览器):协议登录拿 token → list_organizations 发现 org → 填 product/席位。"""
    tag = console.get("name") or console.get("admin_email") or "console"
    import admin_login_protocol
    tok = admin_login_protocol.protocol_login(console, proxy)
    if not tok:
        print(f"[{tag}] 协议登录没拿到 token", flush=True)
        return False
    org = console.get("org_id")
    if not org:
        try:
            orgs = jil.list_organizations(tok)
        except Exception as exc:
            print(f"[{tag}] ❌ 列组织失败: {exc}", flush=True)
            return False
        if not orgs:
            print(f"[{tag}] ❌ 该母号没有可管理的组织", flush=True)
            return False
        org = orgs[0]["id"]
        print(f"[{tag}] 发现 org={org}（{orgs[0].get('name','')}）", flush=True)
    if _fill_product_and_seats(console, org, tok, tag):
        acm._mark_seeded(console)
        return True
    return False


def extract_after_login(console, page, tag):
    """在【已登录】的 page 上提取 org/product/users地址/token/席位 并写入 console。返回 bool。
    手动登录、登录母号 共用这个；自己挂 request 拦截 + 导航 products 页触发 bps-il 拿 token。"""
    _t0 = time.time()
    holder = {"t": None, "org": None, "seen": 0}

    def on_req(req):
        try:
            if "bps-il.adobe.io" in req.url:
                holder["seen"] += 1
                m = re.search(r"/organizations/([^/]+)/", req.url)
                if m and not holder["org"]:
                    holder["org"] = m.group(1)
                a = req.headers.get("authorization", "")
                if not a:
                    try:
                        a = (req.all_headers() or {}).get("authorization", "")
                    except Exception:
                        a = ""
                if a[:7].lower() == "bearer " and not holder["t"]:
                    holder["t"] = a[7:]
        except Exception:
            pass

    page.on("request", on_req)

    def _ims_token():
        # 直接从页面读 IMS 访问令牌（bps-il 的 Bearer 就是它）——秒级、不依赖网络拦截
        try:
            t = page.evaluate(
                "() => {"
                "  try { if (window.adobeIMS && window.adobeIMS.getAccessToken) { var t = window.adobeIMS.getAccessToken(); if (t && t.token) return t.token; } } catch(e){}"
                "  try { for (var i=0;i<localStorage.length;i++){ var k=localStorage.key(i); if(/access_token/i.test(k)){ var v=localStorage.getItem(k)||''; try{var o=JSON.parse(v); if(o&&o.tokenValue) return o.tokenValue; if(o&&o.token) return o.token;}catch(e){} if(v.slice(0,2)==='ey') return v; } } } catch(e){}"
                "  return ''; }"
            )
            return (str(t or "").strip()) or None
        except Exception:
            return None

    def _ims_diag():
        try:
            return page.evaluate("() => { var d={hasIMS: !!(window.adobeIMS&&window.adobeIMS.getAccessToken), keys:[]}; try{ for(var i=0;i<localStorage.length;i++){var k=localStorage.key(i)||''; if(/token|ims/i.test(k)) d.keys.push(k.slice(0,70)); } }catch(e){} return JSON.stringify(d); }")
        except Exception as e:
            return "diag-err:" + str(e)[:60]

    org = console.get("org_id") or _org_from_url(page.url)
    _dbg(tag, f"ENTRY url={str(page.url)[:120]} org={org or '-'} | {_ims_diag()}")

    # ★ 快路径：org 已在 URL + 直接从页面 IMS 读 token（秒级，不重新导航、不等网络拦截）
    tok = _ims_token() if org else None
    if org and tok:
        print(f"[{tag}] ⚡ 快速提取 org={org}（token 从页面 IMS 直读，无需等待）", flush=True)
    _dbg(tag, f"FAST org={org or '-'} ims_tok={'Y' if tok else 'N'}")

    # 慢路径：页面可能还在 root→org 重定向 / IMS 未就绪。耐心轮询最多 ~45s，
    # 三路并取：① URL 的 @AdobeOrg  ② bps-il 拦截(holder)  ③ 页面 IMS 直读(仅在 adminconsole 域，保证 token 作用域对)。
    if not (org and tok):
        # 还没在 adminconsole 上 → 主动导航一次触发重定向
        if "adminconsole.adobe.com" not in (page.url or "").lower():
            try:
                page.goto(ROOT, wait_until="commit", timeout=40000)
            except Exception:
                pass
        _deadline = time.time() + 45
        _last = 0
        while time.time() < _deadline:
            if not org:
                org = holder["org"] or _org_from_url(page.url)
            if not tok:
                _on_ac = "adminconsole.adobe.com" in (page.url or "").lower()
                tok = holder["t"] or (_ims_token() if _on_ac else None)
            if org and tok:
                break
            # 每 ~10s 还没好就再戳一次 overview，逼它发 bps-il / 完成重定向
            if org and (time.time() - _last) > 10:
                _last = time.time()
                try:
                    page.goto(f"{ROOT}{org}/overview", wait_until="commit", timeout=30000)
                except Exception:
                    pass
            time.sleep(1)
        org = org or holder["org"] or _org_from_url(page.url)
        tok = tok or holder["t"] or _ims_token()
        _dbg(tag, f"SLOW org={org or '-'} tok={'Y' if tok else 'N'} bps={holder['seen']} url={str(page.url)[:110]} | {_ims_diag()}")

    # ★有 token 但页面卡在裸根页(没重定向出 @AdobeOrg)→ 用 token 查组织 API 兜底(协议路径已这么做,浏览器路径别白扔可用token)
    if tok and not org:
        try:
            _orgs = jil.list_organizations(tok)
            if _orgs:
                org = _orgs[0]["id"]
                print(f"[{tag}] 页面没给 org,用 token 查组织兜底 → org={org}（{_orgs[0].get('name','')}）", flush=True)
        except Exception as _e:
            print(f"[{tag}] token 查组织兜底失败: {str(_e)[:80]}", flush=True)

    if not (org and tok):
        print(f"[{tag}] ❌ 没抓到 org/token（org={org or '无'} token={'有' if tok else '无'} bps-il请求数={holder['seen']}）", flush=True)
        _dbg(tag, f"FAIL org={org or '-'} tok={'Y' if tok else 'N'} bps={holder['seen']} url={str(page.url)[:120]} elapsed={time.time()-_t0:.1f}s | {_ims_diag()}")
        return False

    return _fill_product_and_seats(console, org, tok, tag)


def discover_one(console, proxy, headless=True):
    """优先纯协议发现 org/product/token(不开浏览器);协议失败再回退浏览器登录提取。"""
    tag = console.get("name") or console.get("admin_email") or "console"
    try:
        if discover_one_protocol(console, proxy):
            return True
        print(f"[{tag}] 协议发现没成,回退浏览器登录提取…", flush=True)
    except Exception as exc:
        print(f"[{tag}] 协议发现异常 {str(exc)[:90]},回退浏览器", flush=True)
    return discover_one_browser(console, proxy, headless=headless)


def discover_one_browser(console, proxy, headless=True):
    tag = console.get("name") or console.get("admin_email") or "console"
    had_url = bool(console.get("product_users_url"))
    with sync_playwright() as p:
        ctx = acm._launch_admin_context(p, proxy, headless=headless, profile_dir=acm._console_profile(console))
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            ok = acm._login_admin(page, ctx, console, proxy, tag, 180,
                                  target=(console.get("product_users_url") or ROOT),
                                  require_products=had_url, max_attempts=1)
            if not ok and not had_url and acm._on_admin_console(page):
                print(f"[{tag}] 已登录到 Admin Console 根页，继续尝试提取 org/token", flush=True)
                ok = True
            if not ok:
                print(f"[{tag}] ❌ 登录失败（密码/验证码/session 问题），无法提取 JSON。"
                      f"可先【手动登录/播种】一次再点登录母号。", flush=True)
                return False
            extracted = extract_after_login(console, page, tag)
            if extracted:
                acm._mark_seeded(console)  # 成功提取 org/token 后才标记已播种
            return extracted
        except Exception as exc:
            print(f"[{tag}] 提取异常: {exc}", flush=True)
            return False
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def run(args):
    cfg, consoles = acm._load_consoles()
    if args.console:
        sel = args.console.strip().lower()
        consoles = [c for c in consoles if sel in str(c.get("name", "")).lower() or sel in str(c.get("admin_email", "")).lower()]
        if not consoles:
            raise RuntimeError(f"没找到匹配 --console={args.console} 的母号")
    elif getattr(args, "only", ""):
        wanted = {e.strip().lower() for e in str(args.only).split(",") if e.strip()}
        consoles = [c for c in consoles
                    if str(c.get("admin_email", "")).strip().lower() in wanted
                    or str(c.get("name", "")).strip().lower() in wanted]
        if not consoles:
            raise RuntimeError("没找到勾选的母号")
        print(f"#### 只处理勾选的 {len(consoles)} 个母号 ####", flush=True)
    elif not getattr(args, "force", False):
        # 批量(无 --console)默认跳过已完成的：有 token + url + 已播种
        kept = []
        for c in consoles:
            done = c.get("jil_token") and c.get("product_users_url") and acm._is_seeded_marker(c)
            if done:
                print(f"[{c.get('name') or c.get('admin_email')}] 已完成(有token+url+已播种)，跳过（--force 可强制重登）", flush=True)
            else:
                kept.append(c)
        consoles = kept
        if not consoles:
            print("#### 没有需要登录的母号(都已完成) ####", flush=True)
            return 0
    proxy = (cfg.get("proxy") or "").strip() or None
    workers = max(1, int(getattr(args, "workers", 1) or 1))

    def task(c):
        print("=" * 60, flush=True)
        print(f"登录并提取 JSON: {c.get('name')} / {c.get('admin_email')}", flush=True)
        try:
            return bool(discover_one(c, proxy, headless=not args.headed))
        except Exception as exc:
            print(f"[{c.get('name') or c.get('admin_email')}] 异常: {exc}", flush=True)
            return False

    # 并发：每个母号各自独立 sync_playwright + 独立 profile，互不冲突；跑完统一保存（避免并发写配置）
    if workers <= 1 or len(consoles) <= 1:
        results = [task(c) for c in consoles]
    else:
        print(f"#### 并发登录 {len(consoles)} 个母号，workers={workers} ####", flush=True)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(task, consoles))

    ok = sum(1 for r in results if r)
    failed = [(c.get("name") or c.get("admin_email")) for c, r in zip(consoles, results) if not r]
    if ok:
        # 只合并保存成功的（加锁 + 重读，多任务并发写 config 不会互相覆盖）
        acm.save_consoles_merge([c for c, r in zip(consoles, results) if r])
        print(f"#### 已保存 {ok}/{len(consoles)} 个母号的 org/product/url/token 到 config ####", flush=True)
    if failed:
        print(f"#### ❌ 登录失败 {len(failed)} 个(不重试，请【手动登录】或检查账密/RT)：{', '.join(failed)} ####", flush=True)
    elif not ok:
        print("#### 没有提取到任何母号 JSON ####", flush=True)
    return 0


def parse_args(argv):
    ap = argparse.ArgumentParser(description="登录母号并自动提取 org/product/url/token")
    ap.add_argument("--console", default="", help="按名字/邮箱只处理某个母号；留空处理全部")
    ap.add_argument("--headed", action="store_true", help="有头模式（首次登录/调试用）")
    ap.add_argument("--workers", type=int, default=1, help="并发数（同时登录几个母号）")
    ap.add_argument("--force", action="store_true", help="批量时也重登已完成的母号（默认跳过）")
    ap.add_argument("--only", default="", help="逗号分隔的管理员邮箱/名称，只处理这些（勾选批量用，不跳过）")
    return ap.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args(sys.argv[1:])))
