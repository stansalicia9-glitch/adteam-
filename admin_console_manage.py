# -*- coding: utf-8 -*-
"""Adobe Admin Console 前半部分自动化：登录管理员 -> 删非管理员席位 -> 添加注册小号。

配套下半部分 firefly_login_extract_cookies.py（登录->选企业->跳过手机号->导出cookie）。

流程（每个 console 顺序处理）：
  1. 用管理员账号登录 adminconsole.adobe.com（验证码用 outlook refresh_token 自动取）
  2. 打开该产品的 users 页（config 里的 product_users_url）
  3. 删掉除管理员外的全部成员，腾出席位
  4. 从 registered_accounts.txt 取小号，添加到该产品（最多 target_seats 个）
  5. 把本次成功添加的账号写入 added_accounts.txt，供下半部分导 cookie

首次务必 headed + --dry-run 跑一遍，确认"删/加"识别正确，再去掉 --dry-run 实跑。
"""
import argparse
import json
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

import firefly_register_yescaptcha as firefly
from safe_file_io import append_line_locked, atomic_write_text, exclusive_file_lock

try:
    import firefly_mail_pool
    DEFAULT_CLIENT_ID = getattr(firefly_mail_pool, "DEFAULT_CLIENT_ID", "")
except Exception:
    DEFAULT_CLIENT_ID = ""

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "admin_console_config.json")
APP_CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
REGISTERED_ACCOUNTS_FILE = os.path.join(BASE_DIR, "registered_accounts.txt")
ADDED_FILE = os.path.join(BASE_DIR, "added_accounts.txt")
ASSIGNED_FILE = os.path.join(BASE_DIR, "assigned_accounts.txt")  # 持久化"已分配"账本，跨管理员/跨次去重，永不清空
ADD_FAILED_FILE = os.path.join(BASE_DIR, "admin_add_failed.txt")
ADMIN_PROFILE = os.path.join(BASE_DIR, "admin_profile")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

_ADMIN_BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-gpu",
    "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
]


def _read_json_file(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


def _resolve_chrome_executable():
    """Find a real Chrome executable before falling back to Playwright's channel lookup."""
    config = _read_json_file(APP_CONFIG_FILE)
    admin_config = _read_json_file(CONFIG_FILE)
    candidates = [
        os.environ.get("ADMIN_CHROME_PATH"),
        os.environ.get("CHROME_PATH"),
        os.environ.get("GOOGLE_CHROME_SHIM"),
        config.get("chrome_executable_path"),
        config.get("chrome_path"),
        config.get("browser_executable_path"),
        admin_config.get("chrome_executable_path"),
        admin_config.get("chrome_path"),
        admin_config.get("browser_executable_path"),
        r"D:\谷歌浏览器\chrome.exe",
        r"D:\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
    ]
    for path in candidates:
        path = str(path or "").strip().strip('"')
        if path and os.path.isfile(path):
            return path
    return ""


def _console_profile(console):
    """每个管理员一个独立持久化 profile 目录，互不串号。"""
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(console.get("admin_email") or console.get("name") or "default"))
    d = os.path.join(ADMIN_PROFILE, key)
    os.makedirs(d, exist_ok=True)
    return d


def _seeded_marker_path(console):
    # 只算路径、不建目录（检查时不应产生副作用）
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(console.get("admin_email") or console.get("name") or "default"))
    return os.path.join(ADMIN_PROFILE, key, ".seeded")


def _mark_seeded(console):
    """登录确认成功后写标记。仅靠 profile 目录判已播种会误报（浏览器一开就建目录）。"""
    try:
        _console_profile(console)  # 确保目录存在
        with open(_seeded_marker_path(console), "w", encoding="utf-8") as f:
            f.write("ok")
    except Exception:
        pass


def _is_seeded_marker(console):
    return os.path.exists(_seeded_marker_path(console))


_MERGE_FIELDS = ("jil_token", "org_id", "product_id", "product_users_url", "seats", "admin_client_id", "admin_session_cookie")


def save_consoles_merge(console_list):
    """并发/多进程安全地把这些 console 的字段合并写回 config：
    加锁 → 重读磁盘最新配置 → 按 admin_email 只更新传入这些 console 的字段 → 写回。
    多个任务/进程同时写 config 时不会互相覆盖（避免 lost update 丢 token）。"""
    console_list = [c for c in (console_list or []) if c]
    if not console_list:
        return
    with exclusive_file_lock(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                disk = json.load(f)
        except Exception:
            return
        idx = {}
        for c in disk.get("consoles", []):
            k = str(c.get("admin_email") or "").strip().lower()
            if k:
                idx[k] = c
        for uc in console_list:
            tgt = idx.get(str(uc.get("admin_email") or "").strip().lower())
            if tgt is None:
                continue
            for fld in _MERGE_FIELDS:
                v = uc.get(fld)
                if v not in (None, "", []):
                    tgt[fld] = v
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(disk, f, indent=2, ensure_ascii=False)


def _launch_admin_context(p, proxy, headless, profile_dir=None):
    """持久化 context：优先用系统新版 Chrome（否则 Adobe 判"浏览器不受支持"会禁用 Add users 弹窗），
    没装则回退内置 Chromium(伪装新版UA)。复用 profile 里播种的 session。"""
    base = dict(
        headless=headless,
        args=_ADMIN_BROWSER_ARGS,
        viewport={"width": 1440, "height": 900},
        locale="en-US",
        timezone_id="America/Los_Angeles",
        proxy=firefly._playwright_proxy_config(proxy) or None,
    )
    pdir = profile_dir or ADMIN_PROFILE
    chrome_path = _resolve_chrome_executable()
    if chrome_path:
        try:
            ctx = p.chromium.launch_persistent_context(pdir, executable_path=chrome_path, **base)
            print(f"[Browser] 使用指定 Chrome: {chrome_path}", flush=True)
            ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            return ctx
        except Exception as exc:
            print(f"[Browser] 指定 Chrome 启动失败({str(exc)[:80]})，尝试系统 Chrome", flush=True)
    try:
        ctx = p.chromium.launch_persistent_context(pdir, channel="chrome", **base)
        print("[Browser] 使用系统 Chrome (channel=chrome)", flush=True)
    except Exception as exc:
        print(f"[Browser] 系统 Chrome 不可用({str(exc)[:50]})，回退内置 Chromium", flush=True)
        ctx = p.chromium.launch_persistent_context(
            pdir,
            executable_path=firefly._bundled_chromium_executable() or None,
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
            **base,
        )
    ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    return ctx


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _wait(page, ms):
    try:
        page.wait_for_timeout(ms)
    except Exception:
        time.sleep(ms / 1000)


def _page_text(page):
    try:
        return page.locator("body").inner_text(timeout=1500) or ""
    except Exception:
        return ""


def _parse_account_line(line):
    raw = str(line or "").strip()
    if not raw or raw.startswith("#"):
        return None
    if "----" in raw:
        parts = raw.split("----")
    else:
        parts = re.split(r"[\s,]+", raw)
    email = (parts[0] if parts else "").strip()
    password = parts[1].strip() if len(parts) > 1 else ""
    email_password = parts[2].strip() if len(parts) > 2 else ""
    if not email or "@" not in email:
        return None
    return {"email": email, "password": password, "email_password": email_password, "raw": raw}


def _load_add_accounts(path, limit=0):
    accounts, seen = [], set()
    if not os.path.exists(path):
        return accounts
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            acc = _parse_account_line(line)
            if not acc:
                continue
            key = acc["email"].lower()
            if key in seen:
                continue
            seen.add(key)
            accounts.append(acc)
            if limit and len(accounts) >= limit:
                break
    return accounts


def _assigned_emails():
    """读持久化"已分配"账本：凡是被加进过任何团队的小号邮箱（小写集合）。"""
    s = set()
    if os.path.exists(ASSIGNED_FILE):
        with open(ASSIGNED_FILE, "r", encoding="utf-8-sig") as f:
            for line in f:
                acc = _parse_account_line(line)
                if acc:
                    s.add(acc["email"].lower())
    return s


def _reserve_accounts(add_file, want, existing_emails, tag):
    """并发安全地原子预占 want 个未分配的小号：在同一把跨进程锁内
    [读已分配账本 → 排除已分配/该团队已有 → 挑 want 个 → 立刻写回已分配(预占)]。
    并发的多个管理员进程会在锁上排队，第二个进来就看到第一个的预占，绝不会重号。"""
    if want <= 0:
        return []
    existing = {str(e).lower() for e in (existing_emails or [])}
    picks = []
    with exclusive_file_lock(ASSIGNED_FILE):
        assigned = _assigned_emails()
        for acc in _load_add_accounts(add_file):
            k = acc["email"].lower()
            if k in assigned or k in existing:
                continue
            picks.append(acc)
            assigned.add(k)
            if len(picks) >= want:
                break
        if picks:
            # 直接写 ASSIGNED_FILE（已在锁内，不能再用 append_line_locked 否则同锁重入）
            with open(ASSIGNED_FILE, "a", encoding="utf-8") as f:
                for acc in picks:
                    f.write(acc["raw"].rstrip("\n") + "\n")
                f.flush()
                os.fsync(f.fileno())
    if picks:
        print(f"[{tag}] 已原子预占 {len(picks)} 个小号: {[a['email'] for a in picks]}", flush=True)
    return picks


def _release_assigned(acc):
    """加失败时把预占的号从已分配账本移除，释放回池可被重试（锁内重写）。"""
    key = acc["email"].lower()
    with exclusive_file_lock(ASSIGNED_FILE):
        if not os.path.exists(ASSIGNED_FILE):
            return
        with open(ASSIGNED_FILE, "r", encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
        kept = [ln for ln in lines if (_parse_account_line(ln) or {}).get("email", "").lower() != key]
        with open(ASSIGNED_FILE, "w", encoding="utf-8") as f:
            f.write("".join(ln + "\n" for ln in kept if ln.strip()))


def _mark_pool_success(acc, tag):
    """加成功后把邮箱池里该号标记 success(已开)，UI 可见，也不会被 acquire 再租。"""
    try:
        import firefly_mail_pool
        firefly_mail_pool.mark_account(acc["email"], "success", f"added to team {tag}")
    except Exception:
        pass


def _load_consoles():
    with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
        cfg = json.load(f)
    consoles = cfg.get("consoles") or []
    for c in consoles:
        c.setdefault("keep_admin_emails", [c.get("admin_email", "")])
        c["keep_admin_emails"] = [str(e).strip().lower() for e in c["keep_admin_emails"] if str(e or "").strip()]
        if not c.get("admin_client_id"):
            c["admin_client_id"] = DEFAULT_CLIENT_ID
    return cfg, consoles


def _new_context(browser):
    context = browser.new_context(
        viewport={"width": 1440, "height": 900},
        locale="en-US",
        timezone_id="America/Los_Angeles",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        ),
    )
    context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    return context


# --------------------------------------------------------------------------- #
# 管理员登录
# --------------------------------------------------------------------------- #
def _make_reg(proxy, tag):
    import config_loader
    return config_loader.ChatGPTRegister(proxy=proxy, tag=tag)


def _admin_email_verify(page, context, console, reg, tag, timeout):
    """管理员登录若触发邮箱验证码，用 outlook refresh_token 自动取码填入。"""
    vp = firefly._find_adobe_email_verification_page(context, preferred_page=page)
    if vp is None and not firefly._page_requires_adobe_email_verification(page):
        return page
    vp = vp or page
    rt = console.get("admin_refresh_token", "")
    cid = console.get("admin_client_id", "") or DEFAULT_CLIENT_ID
    if not rt:
        print(f"[{tag}] 需要邮箱验证码但未配置 admin_refresh_token；headed 模式请手动输入", flush=True)
        return vp
    skip = set()
    try:
        skip = firefly._snapshot_outlook_message_ids(rt, cid)
    except Exception as exc:
        print(f"[{tag}] 取邮箱快照失败（继续轮询最新）: {exc}", flush=True)
    # 触发"继续/重发"以促使 Adobe 发码
    firefly._click_action_button_by_text(vp, [r"^\s*Continue\s*$", r"^\s*Send code\s*$", r"^\s*继续\s*$"], timeout=2500)
    code, link = firefly._wait_for_adobe_email(
        reg, console["admin_email"], timeout=max(timeout, 120),
        outlook_refresh_token=rt, outlook_client_id=cid, page=vp, skip_ids=skip,
    )
    if link:
        print(f"[{tag}] 用验证链接完成验证", flush=True)
        vp.goto(link, wait_until="commit", timeout=20000)
    elif code:
        print(f"[{tag}] 取到验证码 {code}", flush=True)
        firefly._fill_adobe_email_code(vp, code)
        try:
            vp.keyboard.press("Enter")
        except Exception:
            pass
        firefly._click_action_button_by_text(vp, [r"^\s*Continue\s*$", r"^\s*Next\s*$", r"^\s*继续\s*$"], timeout=2500)
    else:
        print(f"[{tag}] 超时未取到管理员验证码", flush=True)
    _wait(vp, 2000)
    return vp


def _on_admin_console(page):
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    return "adminconsole.adobe.com" in url and "auth" not in url


def _code_input_visible(page):
    try:
        return bool(page.evaluate(
            """() => {
                const vis=n=>{const b=n.getBoundingClientRect();const s=getComputedStyle(n);
                  return b.width>0&&b.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&!n.disabled;};
                return Array.from(document.querySelectorAll('input')).some(n=>{
                  if(!vis(n))return false;const t=String(n.type||'').toLowerCase();
                  if(['hidden','checkbox','radio','submit','button','password','email'].includes(t))return false;
                  const a=[n.name,n.id,n.autocomplete,n.placeholder,n.getAttribute('aria-label')].join(' ');
                  const ml=Number(n.maxLength||n.getAttribute('maxlength')||0);const b=n.getBoundingClientRect();
                  return /code|otp|verification|passcode/i.test(a)||ml===1||t==='tel'||(b.width<=90&&b.height>=30);});
            }"""
        ))
    except Exception:
        return False


def _is_profile_chooser(page):
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    txt = _page_text(page).lower()
    return ("profile-chooser" in url or "select a profile" in txt
            or "选择一个配置文件" in txt or "选择要登录的配置文件" in txt or "选择登录的配置文件" in txt)


def _login_ok(page, require_products):
    if not _on_admin_console(page):
        return False
    if require_products:
        return "/products/" in (page.url or "") and _wait_for_users_table(page, timeout=10000)
    # For new consoles we often start at the bare Admin Console root. After a
    # reboot or a half-finished login, that root page can be visible without a
    # usable admin session/token. Only treat a reused session as valid when it
    # has already resolved to a concrete organization URL.
    return bool(re.search(r"[0-9A-Za-z]+@AdobeOrg", str(page.url or "")))


def _login_admin(page, context, console, proxy, tag, timeout, target=None, require_products=True, max_attempts=2):
    """email -> verify-identity -> 密码(col3)/邮箱码 -> 选企业 profile -> 进控制台。
    max_attempts=1 时失败不重试（登录母号批量用，失败即报，不做二次登录）。
    target 留空时用 product_users_url（没有就登 adminconsole 根，配合 require_products=False 用于自动提取JSON）。"""
    import firefly_login_extract_cookies as lx
    target = target or console.get("product_users_url") or "https://adminconsole.adobe.com/"
    account = {
        "email": console["admin_email"],
        "password": console["admin_password"],
        "refresh_token": console.get("admin_refresh_token", ""),
        "client_id": console.get("admin_client_id", "") or DEFAULT_CLIENT_ID,
        "email_password": "",
    }
    for attempt in range(1, max(1, max_attempts) + 1):
        # 导航 adminconsole 可能间歇性 ERR_CONNECTION_TIMED_OUT（代理/网络瞬断）→ 重试 3 次，别一超时就判登录失败
        _nav_ok = False
        for _try in range(3):
            try:
                page.goto(target, wait_until="domcontentloaded", timeout=45000)
                _nav_ok = True
                break
            except Exception as _e:
                print(f"[{tag}] 导航失败(第{_try + 1}/3次): {str(_e)[:70]}，2.5s后重试", flush=True)
                _wait(page, 2500)
        if not _nav_ok:
            print(f"[{tag}] ⚠️ 连续 3 次导航 adminconsole 超时（网络/代理问题），跳过该号", flush=True)
            return False
        _wait(page, 2000)
        # 已播种/session 复用时，从 adminconsole 根重定向到 /{org}@AdobeOrg 要几秒~十几秒。
        # 只等 4s 就判断会误判"没登录"→空转。这里轮询最多 ~18s：落到 org 即认登录态（跑提取）；
        # 落在 account.adobe.com（已登录但页面不对）就跳一次 adminconsole；真在登录/auth 页才进登录流程。
        _did_nav = False
        for _ in range(18):
            if _login_ok(page, require_products):
                print(f"[{tag}] 已是登录态（session 复用）", flush=True)
                return True
            _u = (page.url or "").lower()
            if not _did_nav and "account.adobe.com" in _u:
                _did_nav = True
                print(f"[{tag}] 已登录但在账户页，跳转 adminconsole 取 org", flush=True)
                try:
                    page.goto(target, wait_until="commit", timeout=30000)
                except Exception:
                    pass
            elif ("auth.services" in _u or "ims-na1" in _u or "/signin" in _u) and not _on_admin_console(page):
                break
            _wait(page, 1000)
        if not firefly._fill_adobe_signin_email(page, console["admin_email"], timeout=12000):
            try:
                page.goto("https://account.adobe.com/#/signin", wait_until="domcontentloaded", timeout=30000)
                _wait(page, 1500)
            except Exception:
                pass
        if firefly._fill_adobe_signin_email(page, console["admin_email"], timeout=12000):
            firefly._press_continue(page)
            _wait(page, 2500)
        pw_tries = 0
        for _ in range(16):
            if _on_admin_console(page) and "auth" not in (page.url or "").lower():
                break
            # 绑定备用邮箱/手机号/passkey 等"加强安全"页 → 点 Not now 跳过（点 Continue 会要求填邮箱、报错卡住）
            skipped = lx._click_adobe_not_now_if_present(page, context, tag, timeout=1500)
            if skipped is not None:
                page = skipped
                _wait(page, 1500)
                continue
            if _is_profile_chooser(page):
                print(f"[{tag}] 选企业 profile -> {console.get('name')!r}", flush=True)
                # 原生点击优先（导cookie里实测能秒点 React 卡片），合成点击/按名点击兜底
                if not (lx._pick_team_profile_native(page, tag, timeout=12000)
                        or lx._click_business_profile_option(page, profile_name=console.get("name", ""), timeout=6000)) and console.get("name"):
                    firefly._click_by_text(page, re.escape(console["name"]), timeout=3000)
                _wait(page, 2000)
                continue
            if firefly._visible_password_input_present(page):
                if pw_tries >= 2:
                    break
                # 第1次用主密码(第3列)，第2次(主列报"That's an incorrect password")换备密码(另一列)重试
                pwd = console.get("admin_password") if pw_tries == 0 else (console.get("admin_password_alt") or console.get("admin_password"))
                pw_tries += 1
                print(f"[{tag}] 输入管理员密码（第{pw_tries}次，{'主列' if pw_tries == 1 else '备列'}）", flush=True)
                firefly._fill_adobe_signin_password(page, pwd, timeout=10000)
                firefly._press_continue(page)
                _wait(page, 3500)
                continue
            if firefly._page_requires_adobe_email_verification(page) or _code_input_visible(page):
                print(f"[{tag}] 处理邮箱验证码（复用下半部分健壮取码）", flush=True)
                page = lx._complete_login_verification(page, context, account, proxy, tag, timeout, 0, True, False) or page
                _wait(page, 2500)
                continue
            # "Verify your identity / Stay signed in / Continue" 等中间页
            # Not now/Skip 排在 Continue 前面：有"跳过"就先跳过，避免在"加强安全"页误点 Continue
            firefly._click_action_button_by_text(
                page, [r"^\s*Not now\s*$", r"^\s*Skip\s*$", r"^\s*Maybe later\s*$",
                       r"^\s*Continue\s*$", r"^\s*Yes\s*$", r"^\s*Done\s*$", r"^\s*继续\s*$", r"^\s*跳过\s*$"],
                timeout=2500,
            )
            _wait(page, 2500)
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
        _wait(page, 3000)
        if _login_ok(page, require_products):
            print(f"[{tag}] 登录成功 (attempt {attempt})", flush=True)
            return True
        print(f"[{tag}] 第{attempt}次登录未完成 url={(page.url or '')[:80]}", flush=True)
    return False


# --------------------------------------------------------------------------- #
# 读用户表 / 删除 / 添加
# --------------------------------------------------------------------------- #
def _wait_for_users_table(page, timeout=30000):
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        txt = _page_text(page)
        if ("Add users" in txt or "添加用户" in txt) and ("Email" in txt or "电子邮件" in txt or "@" in txt):
            return True
        _wait(page, 800)
    return False


def _collect_rows(page):
    """返回当前页用户行 [{email, checked}]，并尽量解析每行邮箱。"""
    try:
        return page.evaluate(
            r"""() => {
                const emailRe = /[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}/;
                const rows = Array.from(document.querySelectorAll('table tr, [role="row"]'));
                const out = [];
                for (const row of rows) {
                  const text = (row.innerText || '').trim();
                  const m = text.match(emailRe);
                  if (!m) continue;
                  const cb = row.querySelector('input[type="checkbox"], coral-checkbox, [role="checkbox"]');
                  out.push({ email: m[0], hasCheckbox: !!cb,
                             checked: cb ? (cb.checked === true || cb.getAttribute('aria-checked') === 'true' || cb.hasAttribute('checked')) : false });
                }
                // 去重（同邮箱可能命中多层 row）
                const seen = new Set(); const uniq = [];
                for (const r of out) { const k = r.email.toLowerCase(); if (!seen.has(k)) { seen.add(k); uniq.push(r); } }
                return uniq;
            }"""
        ) or []
    except Exception:
        return []


def _set_row_checkbox(page, email, want=True):
    try:
        return bool(page.evaluate(
            r"""({email, want}) => {
                const emailRe = /[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}/;
                const target = String(email).toLowerCase();
                const rows = Array.from(document.querySelectorAll('table tr, [role="row"]'));
                for (const row of rows) {
                  const text = (row.innerText || '');
                  const m = text.match(emailRe);
                  if (!m || m[0].toLowerCase() !== target) continue;
                  const cb = row.querySelector('input[type="checkbox"], coral-checkbox, [role="checkbox"]');
                  if (!cb) return false;
                  const isChecked = cb.checked === true || cb.getAttribute('aria-checked') === 'true' || cb.hasAttribute('checked');
                  if (isChecked === !!want) return true;
                  const clickTarget = cb.querySelector ? (cb.querySelector('input,[role="checkbox"]') || cb) : cb;
                  clickTarget.scrollIntoView({block:'center'});
                  clickTarget.click();
                  return true;
                }
                return false;
            }""",
            {"email": email, "want": want},
        ))
    except Exception:
        return False


def _remove_non_admin(page, keep_emails, dry_run, tag):
    keep = set(e.lower() for e in keep_emails)
    rows = _collect_rows(page)
    if not rows:
        print(f"[{tag}] 未解析到任何用户行（可能 DOM 结构需调整选择器）", flush=True)
        return []
    to_remove = [r["email"] for r in rows if r["email"].lower() not in keep]
    print(f"[{tag}] 当前 {len(rows)} 用户；保留管理员 {sorted(keep)}；拟删除 {len(to_remove)}: {to_remove}", flush=True)
    if dry_run:
        print(f"[{tag}] [dry-run] 跳过实际删除", flush=True)
        return to_remove
    if not to_remove:
        return []
    checked = []
    for em in to_remove:
        if _set_row_checkbox(page, em, want=True):
            checked.append(em)
        else:
            print(f"[{tag}] 勾选失败: {em}", flush=True)
    _wait(page, 800)
    if not firefly._click_action_button_by_text(page, [r"Remove users", r"Remove user", r"移除用户", r"删除用户"], timeout=4000):
        firefly._click_by_text(page, r"Remove users", timeout=3000)
    _wait(page, 1500)
    # 确认弹窗
    firefly._click_action_button_by_text(page, [r"^\s*Remove users\s*$", r"^\s*Remove\s*$", r"^\s*移除\s*$", r"^\s*确定\s*$"], timeout=5000)
    _wait(page, 3000)
    print(f"[{tag}] 已提交删除 {len(checked)} 个用户", flush=True)
    return checked


def _add_users(page, emails, dry_run, tag):
    if not emails:
        return []
    print(f"[{tag}] 拟添加 {len(emails)}: {[a['email'] for a in emails]}", flush=True)
    if dry_run:
        print(f"[{tag}] [dry-run] 跳过实际添加", flush=True)
        return [a["email"] for a in emails]
    if not firefly._click_action_button_by_text(page, [r"^\s*Add users\s*$", r"^\s*添加用户\s*$"], timeout=5000):
        firefly._click_by_text(page, r"Add users", timeout=4000)
    _wait(page, 2000)
    added = []
    for acc in emails:
        em = acc["email"]
        if not _type_email_into_dialog(page, em, tag):
            print(f"[{tag}] 添加输入失败: {em}", flush=True)
            continue
        added.append(em)
        _wait(page, 700)
    _wait(page, 800)
    # 保存
    saved = firefly._click_action_button_by_text(page, [r"^\s*Save\s*$", r"^\s*Add\s*$", r"^\s*保存\s*$", r"^\s*添加\s*$"], timeout=6000)
    if not saved:
        firefly._click_by_text(page, r"Save", timeout=3000)
    _wait(page, 4000)
    print(f"[{tag}] 已提交添加 {len(added)} 个: {added}", flush=True)
    return added


def _type_email_into_dialog(page, email, tag):
    """在 Add users 弹窗的输入框里输入邮箱并确认成 chip。"""
    try:
        ok = page.evaluate(
            r"""(email) => {
                const inputs = Array.from(document.querySelectorAll('input, [contenteditable="true"]'))
                  .filter(n => {
                    const b = n.getBoundingClientRect();
                    const s = getComputedStyle(n);
                    if (b.width < 60 || b.height < 12 || s.display === 'none' || s.visibility === 'hidden') return false;
                    const a = [n.placeholder, n.getAttribute('aria-label'), n.name, n.id].join(' ').toLowerCase();
                    const t = String(n.type || '').toLowerCase();
                    if (['checkbox','radio','button','submit','hidden'].includes(t)) return false;
                    return a.includes('email') || a.includes('name') || a.includes('user') || a.includes('用户') || a.includes('邮') || t === 'email' || t === 'text' || n.isContentEditable;
                  });
                const el = inputs[inputs.length - 1];
                if (!el) return false;
                el.focus();
                if (el.isContentEditable) { el.textContent = email; }
                else {
                  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                  setter.call(el, email);
                  el.dispatchEvent(new Event('input', {bubbles:true}));
                  el.dispatchEvent(new Event('change', {bubbles:true}));
                }
                return true;
            }""",
            email,
        )
        if not ok:
            return False
        _wait(page, 1200)
        # 优先点下拉里"添加 email"的建议项
        firefly._click_by_text(page, re.escape(email), timeout=2500)
        _wait(page, 400)
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass
        return True
    except Exception as exc:
        print(f"[{tag}] 输入异常 {email}: {exc}", flush=True)
        return False


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #
def process_console(console, add_file, target_seats, proxy, headless, timeout, dry_run, login_only=False):
    tag = console.get("name") or console.get("admin_email") or "console"
    print("=" * 64, flush=True)
    print(f"[{tag}] 处理控制台  product_users_url={console['product_users_url']}", flush=True)
    added_emails = []
    with sync_playwright() as p:
        context = _launch_admin_context(p, proxy, headless, profile_dir=_console_profile(console))
        page = context.pages[0] if context.pages else context.new_page()
        try:
            if not _login_admin(page, context, console, proxy, tag, timeout):
                print(f"[{tag}] 管理员登录失败，跳过", flush=True)
                return []
            if not _wait_for_users_table(page, timeout=30000):
                print(f"[{tag}] 用户表未加载（URL/权限/选择器问题），跳过", flush=True)
                firefly._save_debug_artifacts(page, f"admin_{tag}_no_table")
                return []

            if login_only:
                print(f"[{tag}] ✅ 登录成功，session 已缓存到 {_console_profile(console)}（仅登录，未改动）", flush=True)
                return []

            _remove_non_admin(page, console["keep_admin_emails"], dry_run, tag)
            _wait(page, 2500)

            # 删除后重新读，计算空席并挑未在团队里的小号
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            _wait_for_users_table(page, timeout=20000)
            existing = {r["email"].lower() for r in _collect_rows(page)}
            want = max(0, int(target_seats))

            if dry_run:
                # 预演：不预占，只展示拟加（排除已分配 + 该团队已有）
                assigned = _assigned_emails()
                picks = [a for a in _load_add_accounts(add_file)
                         if a["email"].lower() not in assigned and a["email"].lower() not in existing][:want]
                _add_users(page, picks, True, tag)
                return []

            # 实跑：锁内原子预占（并发也绝不重号）
            picks = _reserve_accounts(add_file, want, existing, tag)
            added_emails = _add_users(page, picks, False, tag)
            added_set = {e.lower() for e in added_emails}
            for acc in picks:
                if acc["email"].lower() in added_set:
                    append_line_locked(ADDED_FILE, acc["raw"])
                    _mark_pool_success(acc, tag)         # 加成功：标记邮箱池 success
                else:
                    _release_assigned(acc)               # 加失败：释放预占，回池可重试
                    print(f"[{tag}] 加失败已释放预占: {acc['email']}", flush=True)
        except Exception as exc:
            print(f"[{tag}] 异常: {exc}", flush=True)
            try:
                firefly._save_debug_artifacts(page, f"admin_{tag}_error")
            except Exception:
                pass
        finally:
            try:
                context.close()
            except Exception:
                pass
    return added_emails


def run(args):
    cfg, consoles = _load_consoles()
    if not consoles:
        raise RuntimeError("admin_console_config.json 里没有 consoles")
    proxy = (args.proxy or cfg.get("proxy") or "").strip() or None
    target_seats = args.seats if args.seats > 0 else int(cfg.get("target_seats_per_console", 9))
    total_pool = len(_load_add_accounts(args.add_file))
    assigned_n = len(_assigned_emails())
    print(f"配置控制台 {len(consoles)} 个；小号池 {total_pool} 个，已分配 {assigned_n} 个 -> 可用 {max(0, total_pool - assigned_n)}；每台目标 {target_seats} 席；dry_run={args.dry_run}", flush=True)
    print("注：拿号走锁内原子预占，多管理员并发也绝不重号。", flush=True)
    if args.reset_added:
        atomic_write_text(ADDED_FILE, "", encoding="utf-8")

    if args.console:
        sel = args.console.strip().lower()
        consoles = [c for c in consoles if sel in str(c.get("name", "")).lower() or sel in str(c.get("admin_email", "")).lower()]
        if not consoles:
            raise RuntimeError(f"没找到匹配 --console={args.console} 的管理员")
    total_added = 0
    for console in consoles[: args.limit] if args.limit else consoles:
        added = process_console(console, args.add_file, target_seats, proxy, args.headless, args.timeout, args.dry_run, login_only=args.login_only)
        total_added += len(added)

    print("#" * 64, flush=True)
    print(f"完成。本次共添加 {total_added} 个账号 -> {ADDED_FILE}", flush=True)
    if not args.dry_run and total_added and args.then_extract:
        print("继续下半部分：登录->选企业->跳过手机号->导出cookie", flush=True)
        import subprocess
        subprocess.call([sys.executable, os.path.join(BASE_DIR, "firefly_login_extract_cookies.py"),
                         "--accounts", ADDED_FILE, "--workers", str(args.workers)]
                        + ([] if args.headless else []))
    return 0


def parse_args(argv):
    ap = argparse.ArgumentParser(description="Adobe Admin Console 批量删/加成员（前半部分）")
    ap.add_argument("--add-file", default=REGISTERED_ACCOUNTS_FILE, help="要添加的小号文件，默认 registered_accounts.txt")
    ap.add_argument("--seats", type=int, default=0, help="每台控制台添加席位数，默认读 config target_seats_per_console")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个控制台")
    ap.add_argument("--console", default="", help="按名字/邮箱筛选只处理某个管理员控制台")
    ap.add_argument("--proxy", default="", help="代理，如 http://127.0.0.1:7890")
    ap.add_argument("--headless", action="store_true", help="无头运行（首次调试别用，先 headed）")
    ap.add_argument("--timeout", type=int, default=180, help="登录/验证码超时秒")
    ap.add_argument("--workers", type=int, default=1, help="传给下半部分的并发")
    ap.add_argument("--dry-run", action="store_true", help="只识别并打印拟删/拟加，不实际操作（强烈建议首次用）")
    ap.add_argument("--login-only", action="store_true", help="只自动登录并缓存 session（不删不加），登录后自动关闭")
    ap.add_argument("--reset-added", action="store_true", help="清空 added_accounts.txt 再写")
    ap.add_argument("--then-extract", action="store_true", help="添加完直接调用下半部分导 cookie")
    args = ap.parse_args(argv)
    args.add_file = os.path.abspath(args.add_file)
    return args


if __name__ == "__main__":
    raise SystemExit(run(parse_args(sys.argv[1:])))
