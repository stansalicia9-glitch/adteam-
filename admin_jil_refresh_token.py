# -*- coding: utf-8 -*-
"""自动刷新母号 JIL token：用已播种的母号 session(admin_profile)打开 Admin Console，
拦截它自己发的 bps-il.adobe.io 请求里的 Authorization Bearer，写回 config 的 jil_token。
省去每24h手动 Copy-as-cURL。前提：该母号已"登录母号/播种"过、session 未失效。
"""
import argparse
import json
import sys
import time

from playwright.sync_api import sync_playwright

import adobe_jil as jil
import admin_console_manage as acm


def _save_cfg(cfg):
    with open(acm.CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def refresh_one(console, proxy):
    tag = console.get("name") or console.get("admin_email") or "console"
    holder = {"t": None}
    with sync_playwright() as p:
        ctx = acm._launch_admin_context(p, proxy, headless=True, profile_dir=acm._console_profile(console))
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_req(req):
            try:
                if "bps-il.adobe.io" in req.url and not holder["t"]:
                    a = req.headers.get("authorization", "")
                    if a[:7].lower() == "bearer ":
                        holder["t"] = a[7:]
            except Exception:
                pass

        page.on("request", on_req)
        last_url = ""
        # 没 product_users_url(还没提取过)时退回 adminconsole 根，避免 goto "" 报 invalid URL
        nav = (console.get("product_users_url") or "").strip() or "https://adminconsole.adobe.com/"
        try:
            page.goto(nav, wait_until="domcontentloaded", timeout=45000)
            for _ in range(40):
                if holder["t"]:
                    break
                time.sleep(1)
            last_url = (page.url or "").lower()
        except Exception as exc:
            print(f"[{tag}] 刷新异常: {exc}", flush=True)
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    # 失败提示放到关闭后、按最终是否拿到 token 判断（晚到的 token 也算成功，不再误报）
    if not holder["t"]:
        if "auth" in last_url:
            print(f"[{tag}] ❌ 母号 session 失效(跳到登录页)，需重新【登录母号/播种】一次", flush=True)
        else:
            print(f"[{tag}] ❌ 未抓到 token(页面没发 bps-il 请求?)", flush=True)
    return holder["t"]


def _log(m):
    print(m, flush=True)


def _token_valid(console):
    url = console.get("product_users_url", "")
    org_id = console.get("org_id") or jil.org_id_from_url(url)
    product_id = console.get("product_id") or jil.product_id_from_url(url)
    token = console.get("jil_token", "")
    if not token:
        return False
    try:
        jil.test_token(org_id, product_id, token)
        return True
    except Exception:
        return False


def ensure_token(console, proxy, log=_log):
    """确保 console 有可用 jil_token。★母号【只用浏览器】(复用已播种 admin_profile session),
    彻底不走任何协议:token 有效就用;失效/缺失就用【已登录的浏览器 session(headless)】拦 bps-il 拿新 token,
    绝不重新登录/接码/走协议。session 失效才需重新【手动登录】。返回 (token_or_None, refreshed_bool)。"""
    # ★每母号专属住宅 IP:本线程后续所有 JIL 调用走这个母号固定住宅出口(避免同 IP 操作一堆母号被批量风控)。
    try:
        import network_proxy as _np
        import adobe_jil as _aj
        _pc = _np.proxy_for_console(console)
        _aj.set_console_proxy(_pc)
        if _pc:
            proxy = _pc
    except Exception:
        pass
    tag = console.get("name") or console.get("admin_email") or "console"
    if _token_valid(console):
        return console.get("jil_token", ""), False
    # ★默认【浏览器优先】:复用已播种 session 拦 bps-il 拿 token(不重新登录/不接码,修"登录态已有还重新登录")
    t = refresh_one(console, proxy)
    if t:
        console["jil_token"] = t
        log(f"[{tag}] ✅ token 已用浏览器 session 刷新(复用已有登录态,没重新登录)")
        return t, True
    # 兜底(协议保留,仅浏览器 session 失效时才用):协议登录接码拿 token
    try:
        import admin_login_protocol
        log(f"[{tag}] 浏览器 session 失效 → 协议兜底登录(接码)…")
        t = admin_login_protocol.protocol_login(console, proxy, log=log)
        if t:
            console["jil_token"] = t
            log(f"[{tag}] ✅ 协议兜底拿到 token")
            return t, True
    except Exception as exc:
        log(f"[{tag}] 协议兜底异常 {str(exc)[:90]}")
    log(f"[{tag}] ❌ 浏览器+协议都没拿到 token → 重新点【手动登录】登一次")
    return None, True


def run(args):
    cfg, consoles = acm._load_consoles()
    if args.console:
        sel = args.console.strip().lower()
        consoles = [c for c in consoles if sel in str(c.get("name", "")).lower() or sel in str(c.get("admin_email", "")).lower()]
        if not consoles:
            raise RuntimeError(f"没找到匹配 --console={args.console} 的母号")
    proxy = (cfg.get("proxy") or "").strip() or None
    refreshed_list = []
    for c in consoles:
        print("=" * 50, flush=True)
        print(f"浏览器刷新 token(默认复用已播种 session): {c.get('name')} / {c.get('admin_email')}", flush=True)
        t = refresh_one(c, proxy)   # ★浏览器优先:复用已登录 session 拦 bps-il
        if not t:
            print(f"[{c.get('name')}] 浏览器没拿到,协议兜底(接码)…", flush=True)
            try:
                import admin_login_protocol
                t = admin_login_protocol.protocol_login(c, proxy)
            except Exception as exc:
                print(f"[{c.get('name')}] 协议兜底异常 {str(exc)[:80]}", flush=True)
                t = ""
        if t:
            c["jil_token"] = t
            refreshed_list.append(c)
            print(f"[{c.get('name')}] ✅ token 已刷新 ({t[:20]}...)", flush=True)
    if refreshed_list:
        acm.save_consoles_merge(refreshed_list)  # 加锁合并，多任务并发不互相覆盖
        print(f"#### 已保存 {len(refreshed_list)} 个母号的新 token 到 config ####", flush=True)
    else:
        print("#### 没有刷新到任何 token ####", flush=True)
    return 0


def parse_args(argv):
    ap = argparse.ArgumentParser(description="自动刷新母号 JIL token")
    ap.add_argument("--console", default="", help="按名字/邮箱只刷某个母号；留空刷全部")
    return ap.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args(sys.argv[1:])))
