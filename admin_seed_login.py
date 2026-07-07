# -*- coding: utf-8 -*-
"""headed 播种登录：打开可见浏览器到管理员控制台，手动登录一次（过验证码/选企业），
session 落进 admin_profile/<管理员>/。之后 admin_console_manage.py 直接复用，永不再登录。

用法：
  python admin_seed_login.py                 # 第一个管理员
  python admin_seed_login.py "Bush"          # 按名字/邮箱选管理员
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
    print(f"播种管理员: {console.get('name')} / {console.get('admin_email')}", flush=True)
    print("浏览器打开后请手动登录：邮箱 -> 验证码(自己邮箱拿) -> 密码(config里admin_password) -> 选企业卡", flush=True)
    with sync_playwright() as p:
        ctx = acm._launch_admin_context(p, proxy, headless=False, profile_dir=acm._console_profile(console))
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        # 新母号还没 product_users_url → 直接开 adminconsole 根（会自动跳登录页）
        target = (console.get("product_users_url") or "").strip() or "https://adminconsole.adobe.com/"
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print("goto:", e, flush=True)
            try:
                page.goto("https://adminconsole.adobe.com/", wait_until="domcontentloaded", timeout=60000)
            except Exception as e2:
                print("goto fallback:", e2, flush=True)
        deadline = time.time() + 900  # 15 分钟内手动登录
        ok = False
        while time.time() < deadline:
            try:
                if not ctx.pages or all(p.is_closed() for p in ctx.pages):
                    print("[SEED_CLOSED] 浏览器窗口被关闭，退出播种。", flush=True)
                    break
                found = None
                for pg in list(ctx.pages):
                    try:
                        if pg.is_closed():
                            continue
                        u = (pg.url or "").lower()
                        # 进了某个 org 的控制台(非登录页)即视为已登录 → session 已落盘
                        # 登录后 adminconsole URL 会含 @adobeorg / overview / products，bare 根/auth 不算
                        if "adminconsole.adobe.com" in u and "auth" not in u and (
                            "@adobeorg" in u or "/overview" in u or "/products/" in u
                        ):
                            found = pg
                            break
                    except Exception:
                        continue
                if found is not None:
                    ok = True
                    break
            except Exception as e:
                if "closed" in str(e).lower():
                    print("[SEED_CLOSED] 浏览器/页面已关闭，退出播种。", flush=True)
                    break
            time.sleep(2)

        if ok:
            acm._mark_seeded(console)  # 真正登录成功才标记已播种
            print("\n[SEED_OK] ✅ 已登录，session 已保存。正在自动提取 org/product/url/token …", flush=True)
            try:
                import admin_login_discover as ald
                ext_page = found if (found is not None and not found.is_closed()) else page
                if ald.extract_after_login(console, ext_page, console.get("name") or console.get("admin_email")):
                    acm.save_consoles_merge([console])
                    print("[SEED_OK] ✅ JSON 已自动提取并保存，无需再点【登录母号】。", flush=True)
                else:
                    print("[SEED_OK] ⚠️ JSON 自动提取没成功，可稍后点【登录母号】重试。", flush=True)
            except Exception as e:
                print(f"[SEED_OK] 自动提取异常: {e}（可稍后点【登录母号】）", flush=True)
            print("现在请【手工关闭】这个浏览器窗口即可。", flush=True)
            # 手动登录模式：不自动关，等用户自己关窗口
            while True:
                try:
                    if not ctx.pages or all(p.is_closed() for p in ctx.pages):
                        break
                except Exception:
                    break
                time.sleep(2)
            print("[SEED_DONE] 窗口已关闭，播种完成。", flush=True)
        else:
            print("\n[SEED_TIMEOUT] 未检测到 users 页（超时）。", flush=True)
        try:
            ctx.close()
        except Exception:
            pass

if __name__ == "__main__":
    main()
