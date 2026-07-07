# -*- coding: utf-8 -*-
"""打开母号页(纯查看):headed 浏览器用母号已播种的 session 直接进 Adobe Admin Console,
看母号真实状态/是否被封/席位/团队 —— 不做检测/提取,看完手工关窗口即可。

用法:
  python admin_open_page.py                 # 第一个母号
  python admin_open_page.py "ellisis"       # 按名字/邮箱选母号
"""
import sys
import time
from playwright.sync_api import sync_playwright
import admin_console_manage as acm


def pick_console(consoles, sel):
    if not sel:
        return consoles[0]
    s = sel.strip().lower()
    for c in consoles:
        if s in str(c.get("name", "")).lower() or s in str(c.get("admin_email", "")).lower():
            return c
    return consoles[0]


def main():
    cfg, consoles = acm._load_consoles()
    if not consoles:
        print("config 里没有 consoles"); return
    sel = sys.argv[1] if len(sys.argv) > 1 else ""
    console = pick_console(consoles, sel)
    proxy = (cfg.get("proxy") or "").strip() or None
    seeded = acm._is_seeded_marker(console) if hasattr(acm, "_is_seeded_marker") else True
    print("打开母号页: %s / %s%s" % (
        console.get("name"), console.get("admin_email"),
        "" if seeded else "  ⚠未播种,会跳登录页、要手动登一次"), flush=True)
    with sync_playwright() as p:
        ctx = acm._launch_admin_context(p, proxy, headless=False, profile_dir=acm._console_profile(console))
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        # 已播种母号:profile 复用 session,直接进已登录 adminconsole;否则跳登录页
        target = (console.get("product_users_url") or "").strip() or "https://adminconsole.adobe.com/"
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print("goto:", e, flush=True)
            try:
                page.goto("https://adminconsole.adobe.com/", wait_until="domcontentloaded", timeout=60000)
            except Exception as e2:
                print("goto fallback:", e2, flush=True)
        print("[OPEN] ✅ 浏览器已打开 —— 自己看母号状态/是否被封(被封会有禁用/suspended 提示);看完【手工关闭】窗口即可。", flush=True)
        # 纯查看:不检测不提取,只等用户关窗口
        while True:
            try:
                if not ctx.pages or all(pg.is_closed() for pg in ctx.pages):
                    break
            except Exception:
                break
            time.sleep(2)
        print("[OPEN_DONE] 窗口已关闭。", flush=True)
        try:
            ctx.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
