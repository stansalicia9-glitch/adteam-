import argparse
import io
import importlib
import os
import re
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from playwright.sync_api import sync_playwright

import firefly_register_yescaptcha as firefly
try:
    import _proxypool  # 复用产号 IP 节点池(每号换干净出口IP,防Adobe per-IP软封)
except Exception:
    _proxypool = None
from safe_file_io import append_line_locked, atomic_write_text, exclusive_file_lock


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ACCOUNTS_FILE = os.path.join(
    BASE_DIR,
    "missing_cookie_accounts.txt" if os.path.exists(os.path.join(BASE_DIR, "missing_cookie_accounts.txt")) else "registered_accounts.txt",
)
FAILED_FILE = os.path.join(BASE_DIR, "login_cookie_extract_failed.txt")
SUCCESS_FILE = os.path.join(BASE_DIR, "login_cookie_extract_success.txt")
MISSING_COOKIE_FILE = os.path.join(BASE_DIR, "missing_cookie_accounts.txt")
REGISTERED_ACCOUNTS_FILE = os.path.join(BASE_DIR, "registered_accounts.txt")
NEWPASS_FILE = os.path.join(BASE_DIR, "team_seats_newpass.txt")  # 强制改密后记录 邮箱----新密码
DEFAULT_FIREFLY_URL = "https://firefly.adobe.com/#"
DEFAULT_PROFILE_NAME = "weifengxinmeiti"


def _parse_account_line(line):
    raw = str(line or "").strip()
    if not raw or raw.startswith("#"):
        return None

    if "----" in raw:
        parts = raw.split("----")
    else:
        parts = re.split(r"[\s,]+", raw)
    parts = [p.strip() for p in parts]
    email = parts[0] if parts else ""
    password = parts[1] if len(parts) > 1 else ""
    email_password = parts[2] if len(parts) > 2 else ""
    status = parts[3] if len(parts) > 3 else ""

    # 5列内联格式: 邮箱----Adobe密码----邮箱密码----client_id----refresh_token
    # 扫描各列识别 client_id(UUID) 与 refresh_token(M.开头/超长)，供导cookie接码用
    client_id = ""
    refresh_token = ""
    for p in parts[2:]:
        if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", p):
            client_id = p
        elif p.startswith("M.") or len(p) > 60:
            refresh_token = p
    if client_id and status == client_id:
        status = ""

    if not email or "@" not in email or not password:
        return None
    return {
        "email": email,
        "password": password,
        # 备列密码：万一某些号 Adobe 密码不在 col2 而在 col3，登录被拒时自动换它重试一次
        "password_alt": email_password,
        "email_password": email_password,
        "status": status,
        "raw": raw,
        "client_id": client_id,
        "refresh_token": refresh_token,
    }


def _load_accounts(path, limit=0):
    accounts = []
    seen = set()
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            account = _parse_account_line(line)
            if not account:
                continue
            key = account["email"].lower()
            if key in seen:
                continue
            seen.add(key)
            accounts.append(account)
            if limit and len(accounts) >= limit:
                break
    return accounts


def _load_accounts_from_mail_pool(limit=0):
    import firefly_mail_pool

    accounts = []
    try:
        target = int(limit or 0)
    except Exception:
        target = 0
    if target <= 0:
        target = firefly_mail_pool.available_count()

    while len(accounts) < target:
        record = firefly_mail_pool.acquire_account()
        if not record:
            break
        email = str(record.get("email") or "").strip()
        password = str(record.get("password") or "").strip()
        email_password = str(record.get("email_password") or "").strip()
        refresh_token = str(record.get("refresh_token") or "").strip()
        client_id = str(record.get("client_id") or firefly_mail_pool.DEFAULT_CLIENT_ID).strip()
        raw = str(record.get("raw") or "----".join([email, password, client_id, refresh_token])).strip()
        if not email or "@" not in email:
            continue
        accounts.append(
            {
                "email": email,
                "password": password,
                "password_alt": email_password,
                "email_password": email_password,
                "client_id": client_id,
                "refresh_token": refresh_token,
                "status": "",
                "raw": raw,
                "from_mail_pool": True,
            }
        )
    return accounts


def _mark_mail_pool_account(account, status, reason=""):
    if not account or not account.get("from_mail_pool"):
        return
    try:
        import firefly_mail_pool

        firefly_mail_pool.mark_account(account.get("email") or "", status, reason)
    except Exception:
        pass


def _complete_mail_pool_account(account, reason=""):
    if not account or not account.get("from_mail_pool"):
        return
    email = str(account.get("email") or "").strip()
    if not email:
        return
    try:
        import firefly_mail_pool

        firefly_mail_pool.mark_account(email, "success", reason)
        deleted = firefly_mail_pool.delete_accounts(emails=[email], mode="selected")
        if deleted:
            print(f"[MailPool] removed completed account: {email}", flush=True)
    except Exception as exc:
        print(f"[MailPool] failed to delete completed account {email}: {exc}", flush=True)


def _load_cookie_email_set():
    with exclusive_file_lock(firefly.COOKIE_JSON_FILE):
        entries = firefly._load_adobe2api_cookie_entries()
    return {
        str(item.get("name") or "").strip().lower()
        for item in entries
        if str(item.get("name") or "").strip()
    }


def _record_extracted_account(account, status="已完成提取"):
    if not account:
        return
    email = str(account.get("email") or "").strip()
    password = str(account.get("password") or "").strip()
    email_password = str(account.get("email_password") or "").strip()
    if not email or "@" not in email or not password:
        return
    try:
        firefly._append_registered_account(
            email,
            password,
            email_password,
            status=status,
        )
    except Exception as exc:
        print(f"[Account] failed to sync extracted account library for {email}: {exc}", flush=True)


def _refresh_missing_cookie_accounts():
    if not os.path.exists(REGISTERED_ACCOUNTS_FILE):
        return 0
    cookie_emails = _load_cookie_email_set()
    missing = []
    seen = set()
    with open(REGISTERED_ACCOUNTS_FILE, "r", encoding="utf-8-sig") as f:
        for line in f:
            account = _parse_account_line(line)
            if not account:
                continue
            key = account["email"].lower()
            if key in seen:
                continue
            seen.add(key)
            if key not in cookie_emails:
                missing.append(account["raw"])
    atomic_write_text(MISSING_COOKIE_FILE, "".join(item + "\n" for item in missing), encoding="utf-8")
    return len(missing)


def _order_cookie_json_by_registered():
    if not os.path.exists(REGISTERED_ACCOUNTS_FILE):
        return {"ordered": 0, "missing": 0, "dropped": 0}

    registered = []
    seen = set()
    with open(REGISTERED_ACCOUNTS_FILE, "r", encoding="utf-8-sig") as f:
        for line in f:
            account = _parse_account_line(line)
            if not account:
                continue
            key = account["email"].lower()
            if key in seen:
                continue
            seen.add(key)
            registered.append((key, account["raw"]))

    with exclusive_file_lock(firefly.COOKIE_JSON_FILE):
        entries = firefly._load_adobe2api_cookie_entries()
        by_name = {}
        for item in entries:
            name = str(item.get("name") or "").strip().lower()
            cookie = str(item.get("cookie") or "").strip()
            if name and cookie:
                by_name[name] = {"name": name, "cookie": cookie}

        ordered = []
        missing = []
        for email, raw in registered:
            item = by_name.get(email)
            if item:
                ordered.append(item)
            else:
                missing.append(raw)

        firefly._write_adobe2api_cookie_entries(ordered)

    atomic_write_text(MISSING_COOKIE_FILE, "".join(item + "\n" for item in missing), encoding="utf-8")
    return {
        "ordered": len(ordered),
        "missing": len(missing),
        "dropped": len(entries) - len(ordered),
    }


def _cookie_exists(email):
    return str(email or "").strip().lower() in _load_cookie_email_set()


def _lookup_outlook_mailbox(email):
    try:
        import firefly_mail_pool

        cfg = firefly_mail_pool.load_config()
        for raw in cfg.get("firefly_outlook_accounts") or []:
            record = firefly_mail_pool.normalize_account(raw)
            if record and record["email"].lower() == email.lower():
                return record
    except Exception:
        return None
    return None


def _make_mail_context(account, proxy, tag):
    import config_loader

    importlib.reload(config_loader)
    reg = config_loader.ChatGPTRegister(proxy=proxy, tag=tag)
    if account.get("refresh_token"):
        return (
            reg,
            account["email"],
            account["refresh_token"],
            account.get("client_id") or "",
            True,
        )
    outlook = _lookup_outlook_mailbox(account["email"])
    if outlook and outlook.get("refresh_token"):
        return reg, account["email"], outlook["refresh_token"], outlook.get("client_id") or "", True

    mail_token = account.get("email_password") or account["email"]
    if not str(mail_token).startswith("cfworker:"):
        domain = account["email"].split("@", 1)[1].lower() if "@" in account["email"] else ""
        cf_domain = str(getattr(config_loader, "CF_EMAIL_DOMAIN", "") or "").strip().lower()
        worker_host = re.sub(r"^https?://", "", str(getattr(config_loader, "CF_WORKER_DOMAIN", "") or "").strip().lower()).split("/", 1)[0]
        if domain and domain in {cf_domain, worker_host, "pengfeiapi.xyz"}:
            mail_token = f"cfworker:{account['email']}:"
        else:
            return reg, "", "", "", False
    return reg, mail_token, "", "", True


def _new_context(browser):
    _kw = dict(
        viewport={"width": 1365, "height": 900},
        locale="en-US",
        timezone_id="America/Los_Angeles",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        ),
    )
    _har = os.environ.get("FF_HAR")  # 设了就录 HAR(抓"加入团队"激活请求用)
    if _har:
        _kw["record_har_path"] = _har
        _kw["record_har_mode"] = "full"
    context = browser.new_context(**_kw)
    context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    if os.environ.get("FF_DUMP_REQ"):  # dump 关键 POST/PUT,排除 audit/passkey/fingerprint 噪音(抓"加入团队"激活 API)
        _SKIP = ("/signin/v1/audit", "/passkey", "fingerprint", "/ee/")
        def _dump(req):
            try:
                u = req.url
                _is_link_get = req.method == "GET" and any(g in u for g in ("filtered_profiles", "/links", "/accounts", "workflows", "status"))
                if (req.method in ("POST", "PUT", "PATCH") or _is_link_get) and not any(s in u for s in _SKIP) and any(d in u for d in (
                        "auth.services", "bps-il", "adobeid", "aps-web", "profile", "provision", "jit", "jil",
                        "/accounts", "/tokens", "/ims/", "switch", "organizations", "members")):
                    body = ""
                    try:
                        body = (req.post_data or "")[:500]
                    except Exception:
                        pass
                    print("[REQ-DUMP] %s %s | body=%s" % (req.method, u[:170], body), flush=True)
            except Exception:
                pass
        context.on("request", _dump)
    return context


# 全局固定等待缩放：流程里几十处 _wait_short 死等是主要延时来源。
# 默认 0.5（全部减半），可用环境变量 FF_WAIT_SCALE 调（如 0.35 更快 / 1.0 还原）。
_WAIT_SCALE = max(0.1, min(1.0, float(os.environ.get("FF_WAIT_SCALE", "0.5"))))


def _wait_short(page, ms):
    ms = int(ms * _WAIT_SCALE)
    try:
        page.wait_for_timeout(ms)
    except Exception:
        time.sleep(ms / 1000)


def _context_pages(context, preferred_page=None):
    pages = []
    if preferred_page is not None:
        pages.append(preferred_page)
    try:
        for item in context.pages:
            if item not in pages:
                pages.append(item)
    except Exception:
        pass
    live = []
    for item in pages:
        try:
            if not item.is_closed():
                live.append(item)
        except Exception:
            pass
    return live


def _page_text(page):
    try:
        return page.locator("body").inner_text(timeout=2500) or ""
    except Exception:
        return ""


def _page_has_any_text(page, patterns):
    text = _page_text(page).lower()
    return any(str(pattern).lower() in text for pattern in patterns)


def _find_best_auth_page(context, preferred_page=None):
    for item in _context_pages(context, preferred_page):
        try:
            url = (item.url or "").lower()
        except Exception:
            continue
        if "auth.services.adobe.com" in url or "account.adobe.com" in url:
            return item
    return None


def _click_visible_text(page, patterns, timeout=8000, button_only=False, exact=False):
    deadline = time.time() + max(timeout, 0) / 1000
    wanted = [str(item) for item in patterns if str(item or "").strip()]
    if not wanted:
        return False
    while time.time() < deadline:
        try:
            result = page.evaluate(
                """({ wanted, buttonOnly, exact }) => {
                    const normalize = (value) => String(value || '').replace(/[\\u200B-\\u200D\\uFEFF]/g, '').replace(/\\s+/g, ' ').trim();
                    const wantedNorm = wanted.map(normalize).filter(Boolean);
                    const wantedLower = wantedNorm.map((item) => item.toLowerCase());
                    const collect = (root, out = []) => {
                      if (!root || !root.querySelectorAll) return out;
                      for (const node of root.querySelectorAll('*')) {
                        out.push(node);
                        if (node.shadowRoot) collect(node.shadowRoot, out);
                      }
                      return out;
                    };
                    const textOf = (node) => normalize(
                      node.innerText
                      || node.textContent
                      || node.value
                      || node.getAttribute?.('aria-label')
                      || node.getAttribute?.('title')
                      || ''
                    );
                    const buttonish = (node) => {
                      const tag = String(node.tagName || '').toLowerCase();
                      const role = String(node.getAttribute?.('role') || '').toLowerCase();
                      const type = String(node.type || node.getAttribute?.('type') || '').toLowerCase();
                      return tag === 'button'
                        || tag === 'a'
                        || tag === 'sp-button'
                        || tag === 'sp-action-button'
                        || tag === 'coral-button'
                        || role === 'button'
                        || role === 'link'
                        || (tag === 'input' && ['submit', 'button'].includes(type));
                    };
                    const visible = (node) => {
                      if (!node || !node.getBoundingClientRect) return false;
                      const box = node.getBoundingClientRect();
                      const style = getComputedStyle(node);
                      return box.width > 0 && box.height > 0
                        && box.right > 0 && box.bottom > 0
                        && box.left < innerWidth && box.top < innerHeight
                        && style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && Number(style.opacity || 1) > 0.05;
                    };
                    const enabled = (node) => {
                      const aria = String(node.getAttribute?.('aria-disabled') || '').toLowerCase();
                      return !node.disabled && aria !== 'true' && !node.hasAttribute?.('disabled');
                    };
                    const matchText = (text) => {
                      const lower = text.toLowerCase();
                      for (let i = 0; i < wantedLower.length; i += 1) {
                        if (exact ? lower === wantedLower[i] : lower.includes(wantedLower[i])) {
                          return wantedNorm[i];
                        }
                      }
                      return '';
                    };
                    const parentOf = (node) => {
                      if (!node) return null;
                      if (node.parentElement) return node.parentElement;
                      const root = node.getRootNode?.();
                      return root && root.host ? root.host : null;
                    };
                    const clickableAncestor = (node) => {
                      for (let cur = node, depth = 0; cur && depth < 8; cur = parentOf(cur), depth += 1) {
                        if (!visible(cur) || !enabled(cur)) continue;
                        if (buttonish(cur)) return cur;
                        if (!buttonOnly) {
                          const role = String(cur.getAttribute?.('role') || '').toLowerCase();
                          const tag = String(cur.tagName || '').toLowerCase();
                          const cursor = getComputedStyle(cur).cursor;
                          if (role === 'option' || role === 'menuitem' || role === 'listitem' || cur.tabIndex >= 0 || cursor === 'pointer') {
                            return cur;
                          }
                          if (['div', 'li', 'span'].includes(tag) && textOf(cur).length <= 260) return cur;
                        }
                      }
                      return null;
                    };
                    const candidates = [];
                    for (const node of collect(document)) {
                      if (!visible(node) || !enabled(node)) continue;
                      const tag = String(node.tagName || '').toLowerCase();
                      if (tag === 'html' || tag === 'body' || tag === 'script' || tag === 'style') continue;
                      const text = textOf(node);
                      if (!text || text.length > 400) continue;
                      const matched = matchText(text);
                      if (!matched) continue;
                      const target = clickableAncestor(node);
                      if (!target || !visible(target) || !enabled(target)) continue;
                      if (buttonOnly && !buttonish(target)) continue;
                      const box = target.getBoundingClientRect();
                      const ownBox = node.getBoundingClientRect();
                      const targetText = textOf(target);
                      candidates.push({
                        target,
                        text,
                        targetText,
                        exact: text.toLowerCase() === matched.toLowerCase(),
                        button: buttonish(target),
                        area: Math.max(1, box.width * box.height),
                        ownArea: Math.max(1, ownBox.width * ownBox.height),
                      });
                    }
                    if (!candidates.length) return { ok: false };
                    candidates.sort((a, b) =>
                      Number(b.exact) - Number(a.exact)
                      || Number(b.button) - Number(a.button)
                      || a.area - b.area
                      || a.ownArea - b.ownArea
                    );
                    const target = candidates[0].target;
                    target.scrollIntoView({ block: 'center', inline: 'center' });
                    const box = target.getBoundingClientRect();
                    const x = Math.min(Math.max(box.left + box.width / 2, 8), innerWidth - 8);
                    const y = Math.min(Math.max(box.top + box.height / 2, 8), innerHeight - 8);
                    try { target.focus({ preventScroll: true }); } catch (_) {}
                    const Pointer = window.PointerEvent || window.MouseEvent;
                    for (const name of ['pointerover', 'mouseover', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                      target.dispatchEvent(new Pointer(name, {
                        bubbles: true,
                        cancelable: true,
                        view: window,
                        clientX: x,
                        clientY: y,
                        button: 0,
                        pointerId: 1,
                        pointerType: 'mouse',
                        isPrimary: true,
                      }));
                    }
                    if (typeof target.click === 'function') target.click();
                    return { ok: true, text: candidates[0].text, targetText: candidates[0].targetText };
                }""",
                {"wanted": wanted, "buttonOnly": bool(button_only), "exact": bool(exact)},
            )
            if result and result.get("ok"):
                return True
        except Exception:
            pass
        _wait_short(page, 400)
    return False


def _click_role_button(page, names, timeout=6000):
    """用 Playwright 原生 get_by_role 点按钮（对 React 才有效，合成点击常无反应）。names 是按钮文字列表。"""
    deadline = time.time() + max(timeout, 0) / 1000
    import re as _re
    while time.time() < deadline:
        for nm in names:
            if time.time() >= deadline:
                break
            try:
                # 每名超时 250ms（按钮在就秒点；不在就快速跳过，避免 ~20名×2s=40s 空等。轮询会重试兜底）
                page.get_by_role("button", name=_re.compile(rf"^\s*{_re.escape(nm)}\s*$", _re.I)).first.click(timeout=250)
                print(f"[Firefly] 原生点击按钮: {nm!r}", flush=True)
                return True
            except Exception:
                pass
        _wait_short(page, 400)
    return False


def _click_text_any(page, names, timeout=4000):
    """按文字原生点击（选项/单选/按钮/链接都行）。exact 优先，再退到包含匹配。"""
    deadline = time.time() + max(timeout, 0) / 1000
    while time.time() < deadline:
        for nm in names:
            if time.time() >= deadline:
                break
            for ex in (True, False):
                try:
                    page.get_by_text(nm, exact=ex).first.click(timeout=300)
                    print(f"[Firefly] 点击选项: {nm!r}", flush=True)
                    return True
                except Exception:
                    pass
        _wait_short(page, 400)
    return False


def _click_action(page, patterns, timeout=6000):
    if firefly._click_action_button_by_text(page, patterns, timeout=timeout):
        return True
    literals = []
    for pattern in patterns:
        text = str(pattern or "")
        text = re.sub(r"^\\s\*\(?", "", text)
        text = re.sub(r"\)?\\s\*\$$", "", text)
        text = text.replace("\\", "")
        text = text.strip("^$()")
        if "|" not in text and text:
            literals.append(text)
    return _click_visible_text(page, literals, timeout=timeout, button_only=True) if literals else False


def _find_center_white_modal_rect(page):
    try:
        from collections import deque
        from PIL import Image

        data = page.screenshot(type="png", full_page=False, timeout=5000)
        img = Image.open(io.BytesIO(data)).convert("RGB")
        width, height = img.size
        pixels = img.load()

        def is_white(x, y):
            r, g, b = pixels[x, y]
            return r >= 245 and g >= 245 and b >= 245

        cx, cy = width // 2, height // 2
        step = 3
        visited = set()
        components = []
        for start_y in range(0, height, step):
            for start_x in range(0, width, step):
                seed = (start_x, start_y)
                if seed in visited or not is_white(start_x, start_y):
                    continue
                queue = deque([seed])
                visited.add(seed)
                min_x = max_x = start_x
                min_y = max_y = start_y
                count = 0
                while queue and count < 250000:
                    x, y = queue.popleft()
                    count += 1
                    min_x = min(min_x, x)
                    max_x = max(max_x, x)
                    min_y = min(min_y, y)
                    max_y = max(max_y, y)
                    for nx, ny in ((x + step, y), (x - step, y), (x, y + step), (x, y - step)):
                        if nx < 0 or ny < 0 or nx >= width or ny >= height:
                            continue
                        key = (nx, ny)
                        if key in visited or not is_white(nx, ny):
                            continue
                        visited.add(key)
                        queue.append(key)

                rect_width = max_x - min_x + 1
                rect_height = max_y - min_y + 1
                if rect_width < 280 or rect_height < 320:
                    continue
                if rect_width > 650 or rect_height > 750:
                    continue
                center_x = min_x + rect_width / 2
                center_y = min_y + rect_height / 2
                if abs(center_x - cx) > max(260, width * 0.25):
                    continue
                if abs(center_y - cy) > max(260, height * 0.35):
                    continue
                components.append({
                    "x": min_x,
                    "y": min_y,
                    "width": rect_width,
                    "height": rect_height,
                    "score": count - (abs(center_x - cx) + abs(center_y - cy)) / 4,
                })
        if not components:
            return None
        components.sort(key=lambda item: item["score"], reverse=True)
        best = components[0]
        return {"x": best["x"], "y": best["y"], "width": best["width"], "height": best["height"]}
    except Exception:
        return None


def _adobe_signin_email_input_visible(page):
    targets = [page]
    try:
        targets.extend(list(page.frames))
    except Exception:
        pass
    script = """() => {
        const collect = (root, out = []) => {
          if (!root || !root.querySelectorAll) return out;
          for (const node of root.querySelectorAll('*')) {
            out.push(node);
            if (node.shadowRoot) collect(node.shadowRoot, out);
          }
          return out;
        };
        const visible = (node) => {
          if (!node || !node.getBoundingClientRect) return false;
          const box = node.getBoundingClientRect();
          const style = getComputedStyle(node);
          return box.width > 0 && box.height > 0
            && style.display !== 'none'
            && style.visibility !== 'hidden'
            && Number(style.opacity || 1) > 0.05;
        };
        return collect(document).some((node) => {
          if (!visible(node)) return false;
          const tag = String(node.tagName || '').toLowerCase();
          if (tag !== 'input' && tag !== 'textarea') return false;
          const type = String(node.type || '').toLowerCase();
          const attrs = [
            node.id,
            node.name,
            node.placeholder,
            node.autocomplete,
            node.getAttribute?.('aria-label'),
            node.getAttribute?.('title'),
          ].join(' ').toLowerCase();
          return type === 'email'
            || attrs.includes('email')
            || attrs.includes('username')
            || attrs.includes('adobe id');
        });
    }"""
    for target in targets:
        try:
            if target.evaluate(script):
                return True
        except Exception:
            pass
    return False


def _find_adobe_light_iframe_rect(page):
    script = """() => {
        const collect = (root, out = []) => {
          if (!root || !root.querySelectorAll) return out;
          for (const node of root.querySelectorAll('*')) {
            out.push(node);
            if (node.shadowRoot) collect(node.shadowRoot, out);
          }
          return out;
        };
        const visible = (node) => {
          if (!node || !node.getBoundingClientRect) return false;
          const box = node.getBoundingClientRect();
          const style = getComputedStyle(node);
          return box.width > 0 && box.height > 0
            && style.display !== 'none'
            && style.visibility !== 'hidden'
            && Number(style.opacity || 1) > 0.05;
        };
        const matches = [];
        for (const node of collect(document)) {
          if (String(node.tagName || '').toLowerCase() !== 'iframe') continue;
          const src = String(node.src || node.getAttribute?.('src') || '');
          if (!/auth-light\\.identity\\.adobe\\.com/i.test(src)) continue;
          if (!visible(node)) continue;
          const box = node.getBoundingClientRect();
          matches.push({
            src,
            x: box.left,
            y: box.top,
            width: box.width,
            height: box.height,
          });
        }
        if (!matches.length) return null;
        matches.sort((a, b) => (b.width * b.height) - (a.width * a.height));
        return matches[0];
    }"""
    try:
        return page.evaluate(script)
    except Exception:
        return None


def _click_adobe_light_dialog_sign_in(page):
    script = """() => {
        const roots = [];
        const collectRoots = (root) => {
          if (!root || !root.querySelectorAll) return;
          for (const node of root.querySelectorAll('*')) {
            if (node.shadowRoot) {
              roots.push(node.shadowRoot);
              collectRoots(node.shadowRoot);
            }
          }
        };
        const visible = (node) => {
          if (!node || !node.getBoundingClientRect) return false;
          const box = node.getBoundingClientRect();
          const style = getComputedStyle(node);
          return box.width > 0 && box.height > 0
            && style.display !== 'none'
            && style.visibility !== 'hidden'
            && Number(style.opacity || 1) > 0.05;
        };
        const textOf = (node) => String(
          node?.innerText
          || node?.textContent
          || node?.value
          || node?.getAttribute?.('aria-label')
          || ''
        ).replace(/\\s+/g, ' ').trim();
        const largeButtons = document.querySelector('large-buttons');
        if (largeButtons?.shadowRoot) {
          roots.push(largeButtons.shadowRoot);
          collectRoots(largeButtons.shadowRoot);
        }
        collectRoots(document);
        if (!roots.length) return { ok: false, reason: 'no-shadow-root' };

        const pickClickable = (node) => {
          const direct = [
            node?.shadowRoot?.querySelector?.('a,button,[role="link"],[role="button"]'),
            node?.querySelector?.('a,button,[role="link"],[role="button"]'),
            node,
          ].filter(Boolean);
          for (const item of direct) {
            if (visible(item)) return item;
          }
          return null;
        };

        for (const root of roots) {
          const candidates = [
            root.getElementById?.('sign-in'),
            root.querySelector?.('sp-link#sign-in'),
            root.querySelector?.('footer sp-link#sign-in'),
            root.querySelector?.('#sign-in'),
          ].filter(Boolean);
          for (const host of candidates) {
            if (!visible(host)) continue;
            try {
              host.scrollIntoView({ block: 'center', inline: 'center' });
            } catch (_) {}
            const target = pickClickable(host) || host;
            const box = target.getBoundingClientRect();
            return {
              ok: true,
              tag: String(target.tagName || ''),
              hostTag: String(host.tagName || ''),
              text: textOf(target),
              hostText: textOf(host),
              x: box.left + box.width / 2,
              y: box.top + box.height / 2,
              width: box.width,
              height: box.height,
            };
          }
        }
        return { ok: false, reason: 'sign-in-link-not-visible' };
    }"""
    deadline = time.time() + 8
    last_reason = ""
    while time.time() < deadline:
        iframe_rect = _find_adobe_light_iframe_rect(page)
        frame_match = None
        for frame in getattr(page, "frames", []) or []:
            try:
                url = (frame.url or "").lower()
            except Exception:
                url = ""
            if "auth-light.identity.adobe.com" in url:
                frame_match = frame
                break
        if not iframe_rect or frame_match is None:
            _wait_short(page, 250)
            continue
        try:
            result = frame_match.evaluate(script)
        except Exception as exc:
            last_reason = str(exc)
            _wait_short(page, 250)
            continue
        if not (result and result.get("ok")):
            last_reason = (result or {}).get("reason", "sign-in-link-not-ready")
            _wait_short(page, 250)
            continue
        click_x = float(iframe_rect["x"]) + float(result["x"])
        click_y = float(iframe_rect["y"]) + float(result["y"])
        try:
            page.mouse.move(click_x, click_y, steps=8)
            _wait_short(page, 120)
            page.mouse.click(click_x, click_y, delay=75)
        except Exception as exc:
            last_reason = str(exc)
            _wait_short(page, 250)
            continue
        print(
            f"[Modal] clicking auth-light sign-in inside iframe "
            f"target={result.get('tag', '')}/{result.get('hostTag', '')} "
            f"text={result.get('text') or result.get('hostText')!r} "
            f"x={click_x:.0f} y={click_y:.0f}",
            flush=True,
        )
        return True
    if last_reason:
        print(f"[Modal] auth-light sign-in not ready: {last_reason}", flush=True)
    return False


def _click_adobe_email_entry_choice(page, context=None, timeout=10000):
    deadline = time.time() + max(timeout, 0) / 1000

    def progressed():
        if context is not None and _find_best_auth_page(context, preferred_page=page) is not None:
            return True
        return _adobe_signin_email_input_visible(page)

    last_modal_notice = 0
    while time.time() < deadline:
        if _click_adobe_light_dialog_sign_in(page):
            for _ in range(6):
                _wait_short(page, 500)
                if progressed():
                    return True
        rect = _find_center_white_modal_rect(page)
        if rect:
            click_x = float(rect["x"]) + float(rect["width"]) * 0.43
            click_y = float(rect["y"]) + float(rect["height"]) * 0.79
            print(
                f"[Modal] clicking bottom Sign in link by screenshot "
                f"x={click_x:.0f} y={click_y:.0f}",
                flush=True,
            )
            page.mouse.click(click_x, click_y)
            _wait_short(page, 1200)
            if progressed():
                return True
        now = time.time()
        if now - last_modal_notice >= 4:
            print("[Modal] waiting for visual sign-in modal", flush=True)
            last_modal_notice = now
    return False


def _code_input_visible(page):
    try:
        return bool(page.evaluate(
            """() => {
                const visible = (node) => {
                  const box = node.getBoundingClientRect();
                  const style = getComputedStyle(node);
                  return box.width > 0 && box.height > 0
                    && style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && !node.disabled;
                };
                return Array.from(document.querySelectorAll('input')).some((node) => {
                  if (!visible(node)) return false;
                  const type = String(node.type || '').toLowerCase();
                  if (['hidden', 'checkbox', 'radio', 'submit', 'button', 'password', 'email'].includes(type)) return false;
                  const attrs = [
                    node.name,
                    node.id,
                    node.autocomplete,
                    node.placeholder,
                    node.getAttribute('aria-label'),
                  ].join(' ');
                  const maxLength = Number(node.maxLength || node.getAttribute('maxlength') || 0);
                  const box = node.getBoundingClientRect();
                  return /code|otp|verification|passcode/i.test(attrs)
                    || maxLength === 1
                    || type === 'tel'
                    || (box.width <= 90 && box.height >= 30);
                });
            }"""
        ))
    except Exception:
        return False


def _click_identity_continue(page):
    if not _page_has_any_text(page, ["verify your identity", "verify your email", "验证您的身份", "验证你的身份"]):
        return False
    if _code_input_visible(page):
        return False
    clicked = _click_action(
        page,
        [
            r"^\s*Continue\s*$",
            r"^\s*Next\s*$",
            r"^\s*继续\s*$",
            r"^\s*下一步\s*$",
        ],
        timeout=5000,
    )
    if clicked:
        _wait_short(page, 1500)
    return clicked


def _snapshot_verification_message_ids(reg, mail_token, outlook_refresh_token="", outlook_client_id=""):
    if outlook_refresh_token:
        return firefly._snapshot_outlook_message_ids(outlook_refresh_token, outlook_client_id)
    cf_email, cf_token = firefly._parse_cfworker_mail_token(mail_token)
    if cf_email:
        return firefly._snapshot_cfworker_message_ids(reg, cf_email, cf_token)
    try:
        messages = reg._fetch_emails_duckmail(mail_token) or []
    except Exception:
        return set()
    return {
        str(msg.get("id") or msg.get("@id") or msg.get("message_id") or "").strip()
        for msg in messages
        if str(msg.get("id") or msg.get("@id") or msg.get("message_id") or "").strip()
    }


def _adobe_verification_retry_reason(page):
    text = _page_text(page)
    if not text:
        return ""
    checks = [
        (r"code(?:\s+has|\s+is)?\s+expired|expired\s+code|verification\s+code\s+expired|验证码已过期|代码已过期", "expired"),
        (r"invalid\s+code|invalid\s+verification\s+code|enter\s+a\s+valid\s+code|not\s+a\s+valid\s+code|incorrect\s+code|wrong\s+code|try\s+again|couldn.?t\s+verify|unable\s+to\s+verify|验证码无效|验证码错误|代码无效|请输入有效", "invalid"),
    ]
    for pattern, reason in checks:
        if re.search(pattern, text, re.I):
            return reason
    return ""


def _wait_for_verification_submit_result(page, context, timeout=25000):
    deadline = time.time() + max(timeout, 0) / 1000
    current_page = page
    while time.time() < deadline:
        session_page = firefly._find_firefly_session_page(context, preferred_page=current_page)
        if session_page is not None and firefly._firefly_ready_for_cookie_export(session_page):
            return "accepted", session_page, ""

        verification_page = firefly._find_adobe_email_verification_page(context, preferred_page=current_page)
        if verification_page is not None:
            current_page = verification_page
        if not firefly._page_requires_adobe_email_verification(current_page):
            confirmed = firefly._confirm_adobe_email_verification_completed(current_page, context, timeout=5000)
            return "accepted", confirmed or current_page, ""

        retry_reason = _adobe_verification_retry_reason(current_page)
        if retry_reason:
            return "retry", current_page, retry_reason

        _wait_short(current_page, 1000)

    verification_page = firefly._find_adobe_email_verification_page(context, preferred_page=current_page)
    if verification_page is not None:
        current_page = verification_page
    if not firefly._page_requires_adobe_email_verification(current_page):
        confirmed = firefly._confirm_adobe_email_verification_completed(current_page, context, timeout=5000)
        return "accepted", confirmed or current_page, ""
    retry_reason = _adobe_verification_retry_reason(current_page) or "verification page did not advance after code submit"
    return "retry", current_page, retry_reason


def _open_firefly_login_popup(page, context, tag):
    try:
        page.goto(DEFAULT_FIREFLY_URL, wait_until="commit", timeout=30000)
    except Exception as exc:
        print(f"[{tag}] Firefly open failed before login: {exc}", flush=True)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    _wait_short(page, 1500)
    firefly._dismiss_firefly_popups(page)

    existing = _find_best_auth_page(context, preferred_page=page)
    if existing is not None:
        return existing

    print(f"[{tag}] opening Firefly login dialog", flush=True)
    login_clicked = _click_action(
        page,
        [
            r"^\s*Sign in\s*$",
            r"^\s*Log in\s*$",
            r"^\s*Login\s*$",
            r"^\s*登录\s*$",
            r"^\s*登入\s*$",
        ],
        timeout=15000,
    )
    if not login_clicked:
        login_clicked = _click_visible_text(page, ["登录", "Sign in", "Log in", "Login"], timeout=8000, button_only=True)
    if not login_clicked:
        login_clicked = _click_visible_text(
            page,
            ["立即购买", "查看计划", "Buy now", "View plans", "See plans"],
            timeout=5000,
            button_only=True,
        )
    _wait_short(page, 1500)

    for _ in range(12):
        existing = _find_best_auth_page(context, preferred_page=page)
        if existing is not None:
            return existing
        _wait_short(page, 500)

    if os.environ.get("FIREFLY_ADOBEIMS_SIGNIN_FALLBACK", "").strip() == "1":
        try:
            invoked = page.evaluate(
                """() => {
                    if (window.adobeIMS && typeof window.adobeIMS.signIn === 'function') {
                        window.adobeIMS.signIn();
                        return true;
                    }
                    return false;
                }"""
            )
            if invoked:
                print(f"[{tag}] invoked Firefly adobeIMS sign-in", flush=True)
                for _ in range(24):
                    existing = _find_best_auth_page(context, preferred_page=page)
                    if existing is not None:
                        return existing
                    _wait_short(page, 500)
        except Exception:
            pass

    popup = None
    try:
        with context.expect_page(timeout=12000) as popup_info:
            print(f"[{tag}] choosing Adobe email sign-in/create entry", flush=True)
            _click_adobe_email_entry_choice(page, context=context, timeout=10000)
        popup = popup_info.value
    except Exception:
        print(f"[{tag}] choosing Adobe email sign-in/create entry", flush=True)
        _click_adobe_email_entry_choice(page, context=context, timeout=5000)

    if popup is None:
        for _ in range(20):
            popup = _find_best_auth_page(context, preferred_page=page)
            if popup is not None:
                break
            _wait_short(page, 500)
    if popup is None:
        return None
    try:
        popup.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    try:
        popup.bring_to_front()
    except Exception:
        pass
    return popup


def _complete_login_verification(page, context, account, proxy, tag, timeout, manual_timeout, auto_email, manual):
    verification_page = firefly._find_adobe_email_verification_page(context, preferred_page=page)
    if verification_page is None:
        return page

    if not auto_email:
        return _complete_verification(
            verification_page,
            context,
            account,
            proxy,
            tag,
            timeout,
            manual_timeout,
            auto_email,
            manual,
        )

    reg, mail_token, outlook_refresh_token, outlook_client_id, can_poll = _make_mail_context(account, proxy, tag)
    if not can_poll:
        print(f"[{tag}] no usable mailbox access for automatic Adobe verification", flush=True)
        return _complete_verification(
            verification_page,
            context,
            account,
            proxy,
            tag,
            timeout,
            manual_timeout,
            False,
            manual,
        )

    print(f"[{tag}] Adobe identity verification detected; requesting newest code", flush=True)
    # 只认本次验证开始前 60s 之后的邮件 → 彻底排除上一次留下的 stale 验证码
    fresh_after_ts = time.time() - 60
    skip_ids = set()
    try:
        skip_ids = _snapshot_verification_message_ids(
            reg,
            mail_token,
            outlook_refresh_token=outlook_refresh_token,
            outlook_client_id=outlook_client_id,
        )
    except Exception as exc:
        print(f"[{tag}] mailbox snapshot failed; will still poll newest Adobe mail: {exc}", flush=True)
    if skip_ids:
        print(f"[Mail] ignoring {len(skip_ids)} existing Adobe verification message(s)", flush=True)

    continue_clicked = _click_identity_continue(verification_page)
    if continue_clicked:
        # 点了 "Continue" → 触发本次发码，新鲜度锚定到这一刻，旧码全部跳过
        resend_triggered = (time.time(), 1)
        fresh_after_ts = time.time() - 12
    else:
        # 页面已在"输入我们刚发送的验证码"页 → 码已自动发出且有效。
        # 关键：绝不能点 Resend！否则会作废这个有效码、并制造 A/B 竞态（先到的旧码 A 已被作废 → invalid）。
        # 直接抓最新码；只接受最近 45s 内的邮件以排除上一次的 stale 码；迟迟收不到才由 _maybe_resend 兜底重发。
        resend_triggered = False
        fresh_after_ts = time.time() - 45
        skip_ids = set()
        print("[Mail] 验证码已自动发送，直接抓取最新码（不点 Resend，避免作废有效码）", flush=True)
    blocked_codes = set()
    overall_deadline = time.time() + max(timeout, 240)
    max_attempts = 3

    for attempt in range(1, max_attempts + 1):
        remaining = max(1, int(overall_deadline - time.time()))
        if remaining <= 0:
            break

        code, link = firefly._wait_for_adobe_email(
            reg,
            mail_token,
            timeout=remaining,
            outlook_refresh_token=outlook_refresh_token,
            outlook_client_id=outlook_client_id,
            page=verification_page,
            skip_ids=skip_ids,
            resend_triggered=resend_triggered,
            blocked_codes=blocked_codes,
            fresh_after_ts=fresh_after_ts,
        )
        resend_triggered = False
        verification_page = firefly._find_adobe_email_verification_page(context, preferred_page=verification_page) or verification_page

        if link:
            print(f"[{tag}] Adobe verification link found; opening it", flush=True)
            verification_page.goto(link, wait_until="commit", timeout=20000)
        elif code:
            print(f"[{tag}] Adobe verification code found: {code}", flush=True)
            if not firefly._fill_adobe_email_code(verification_page, code):
                return None
            try:
                verification_page.keyboard.press("Enter")
            except Exception:
                pass
            _click_action(
                verification_page,
                [r"^\s*Continue\s*$", r"^\s*Next\s*$", r"^\s*继续\s*$", r"^\s*下一步\s*$"],
                timeout=2500,
            )
        else:
            print(f"[{tag}] Adobe verification code was not found before timeout", flush=True)
            return None

        outcome, result_page, reason = _wait_for_verification_submit_result(verification_page, context, timeout=25000)
        if outcome == "accepted":
            _wait_short(result_page, 1500)
            session_page = firefly._find_firefly_session_page(context, preferred_page=result_page)
            return session_page or _find_best_auth_page(context, preferred_page=result_page) or result_page

        if code:
            blocked_codes.add(code)
        print(f"[{tag}] Adobe verification attempt was rejected ({reason}); requesting a fresh code", flush=True)
        try:
            skip_ids.update(
                _snapshot_verification_message_ids(
                    reg,
                    mail_token,
                    outlook_refresh_token=outlook_refresh_token,
                    outlook_client_id=outlook_client_id,
                )
            )
        except Exception as exc:
            print(f"[{tag}] mailbox snapshot after rejected code failed: {exc}", flush=True)

        if attempt >= max_attempts or time.time() >= overall_deadline:
            return None

        resend_clicked = firefly._click_adobe_resend_code(result_page)
        resend_triggered = (time.time(), 1) if resend_clicked else False
        verification_page = firefly._find_adobe_email_verification_page(context, preferred_page=result_page) or result_page

    return None


def _page_needs_firefly_onboarding(page):
    return _page_has_any_text(
        page,
        [
            "join team",
            "加入团队",
            "welcome to",
            "欢迎使用",
            "move files",
            "move your files",
            "would you like to move",
            "是否要移动文件",
            "personal cloud storage",
            "个人云存储",
            "confirm your storage",
            "确认您的存储选择",
            "choose a profile",
            "select a profile",
            "选择一个配置文件",
            DEFAULT_PROFILE_NAME,
        ],
    )


def _click_keep_personal_cloud_storage(page, timeout=8000):
    deadline = time.time() + max(timeout, 0) / 1000
    labels = [
        "Keep files in personal cloud storage",
        "keep files in personal",
        "personal cloud storage",
        "将文件保存在个人云存储中",
        "将文件保留在个人云存储中",
    ]
    while time.time() < deadline:
        if _click_visible_text(page, labels, timeout=1200):
            return True
        try:
            clicked = page.evaluate(
                """() => {
                    const normalize = (value) => String(value || '')
                      .replace(/[\\u200B-\\u200D\\uFEFF]/g, '')
                      .replace(/\\s+/g, ' ')
                      .trim();
                    const textOf = (node) => normalize(
                      node?.innerText
                      || node?.textContent
                      || node?.getAttribute?.('aria-label')
                      || node?.getAttribute?.('title')
                      || ''
                    );
                    const visible = (node) => {
                      if (!node || !node.getBoundingClientRect) return false;
                      const box = node.getBoundingClientRect();
                      const style = getComputedStyle(node);
                      return box.width > 0 && box.height > 0
                        && box.right > 0 && box.bottom > 0
                        && box.left < innerWidth && box.top < innerHeight
                        && style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && Number(style.opacity || 1) > 0.05;
                    };
                    const clickable = (node) => {
                      for (let cur = node; cur; cur = cur.parentElement) {
                        if (!visible(cur)) continue;
                        const tag = String(cur.tagName || '').toLowerCase();
                        const role = String(cur.getAttribute?.('role') || '').toLowerCase();
                        const cursor = getComputedStyle(cur).cursor;
                        if (tag === 'button' || tag === 'a' || role === 'button' || role === 'link' || cur.tabIndex >= 0 || cursor === 'pointer') {
                          return cur;
                        }
                      }
                      return node;
                    };
                    const nodes = Array.from(document.querySelectorAll('*')).filter(visible);
                    const targetText = nodes.find((node) => {
                      const lower = textOf(node).toLowerCase();
                      return lower.includes('keep files') && lower.includes('personal cloud storage');
                    }) || nodes.find((node) => {
                      const lower = textOf(node).toLowerCase();
                      return lower.includes('personal cloud storage') && !lower.includes('business cloud storage');
                    });
                    if (!targetText) return false;
                    const target = clickable(targetText);
                    target.scrollIntoView({ block: 'center', inline: 'center' });
                    const box = target.getBoundingClientRect();
                    const x = Math.min(Math.max(box.left + box.width / 2, 8), innerWidth - 8);
                    const y = Math.min(Math.max(box.top + box.height / 2, 8), innerHeight - 8);
                    for (const name of ['pointerover', 'mouseover', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                      target.dispatchEvent(new MouseEvent(name, {
                        bubbles: true,
                        cancelable: true,
                        view: window,
                        clientX: x,
                        clientY: y,
                        button: 0,
                      }));
                    }
                    if (typeof target.click === 'function') target.click();
                    return true;
                }"""
            )
            if clicked:
                return True
        except Exception:
            pass
        _wait_short(page, 400)
    return False


def _click_first_profile_option(page, timeout=8000):
    deadline = time.time() + max(timeout, 0) / 1000
    while time.time() < deadline:
        try:
            clicked = page.evaluate(
                """() => {
                    const normalize = (value) => String(value || '')
                      .replace(/[\\u200B-\\u200D\\uFEFF]/g, '')
                      .replace(/\\s+/g, ' ')
                      .trim();
                    const textOf = (node) => normalize(
                      node?.innerText
                      || node?.textContent
                      || node?.getAttribute?.('aria-label')
                      || node?.getAttribute?.('title')
                      || ''
                    );
                    const visible = (node) => {
                      if (!node || !node.getBoundingClientRect) return false;
                      const box = node.getBoundingClientRect();
                      const style = getComputedStyle(node);
                      return box.width > 0 && box.height > 0
                        && box.right > 0 && box.bottom > 0
                        && box.left < innerWidth && box.top < innerHeight
                        && style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && Number(style.opacity || 1) > 0.05;
                    };
                    const buttonish = (node) => {
                      const tag = String(node.tagName || '').toLowerCase();
                      const role = String(node.getAttribute?.('role') || '').toLowerCase();
                      const cursor = getComputedStyle(node).cursor;
                      return tag === 'button'
                        || tag === 'a'
                        || role === 'button'
                        || role === 'link'
                        || node.tabIndex >= 0
                        || cursor === 'pointer';
                    };
                    const parentOf = (node) => {
                      if (!node) return null;
                      if (node.parentElement) return node.parentElement;
                      const root = node.getRootNode?.();
                      return root && root.host ? root.host : null;
                    };
                    const clickableAncestor = (node) => {
                      for (let cur = node, depth = 0; cur && depth < 8; cur = parentOf(cur), depth += 1) {
                        if (!visible(cur)) continue;
                        if (buttonish(cur)) return cur;
                      }
                      return null;
                    };
                    const excluded = [
                      /select a profile/i,
                      /learn more about profiles/i,
                      /sign in to a different account/i,
                      /^email address$/i,
                      /copyright/i,
                      /terms of use/i,
                      /cookie preferences/i,
                      /privacy/i,
                      /do not sell/i,
                    ];
                    const emailLike = /^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/i;
                    const seen = new Set();
                    const candidates = [];
                    for (const node of Array.from(document.querySelectorAll('*'))) {
                      if (!visible(node)) continue;
                      const text = textOf(node);
                      if (!text || text.length > 160) continue;
                      if (excluded.some((rule) => rule.test(text))) continue;
                      if (emailLike.test(text)) continue;
                      const target = clickableAncestor(node);
                      if (!target || !visible(target)) continue;
                      const box = target.getBoundingClientRect();
                      if (box.width < Math.min(innerWidth * 0.6, 260) || box.height < 56) continue;
                      if (box.top < 120 || box.bottom > innerHeight - 70) continue;
                      const key = `${Math.round(box.left)}:${Math.round(box.top)}:${Math.round(box.width)}:${Math.round(box.height)}`;
                      if (seen.has(key)) continue;
                      seen.add(key);
                      candidates.push({
                        target,
                        text,
                        top: box.top,
                        left: box.left,
                        width: box.width,
                        height: box.height,
                      });
                    }
                    candidates.sort((a, b) =>
                      a.top - b.top
                      || a.left - b.left
                      || (b.width * b.height) - (a.width * a.height)
                    );
                    const first = candidates[0];
                    if (!first) return false;
                    first.target.scrollIntoView({ block: 'center', inline: 'center' });
                    const box = first.target.getBoundingClientRect();
                    const x = Math.min(Math.max(box.left + box.width / 2, 8), innerWidth - 8);
                    const y = Math.min(Math.max(box.top + box.height / 2, 8), innerHeight - 8);
                    for (const name of ['pointerover', 'mouseover', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                      first.target.dispatchEvent(new MouseEvent(name, {
                        bubbles: true,
                        cancelable: true,
                        view: window,
                        clientX: x,
                        clientY: y,
                        button: 0,
                      }));
                    }
                    if (typeof first.target.click === 'function') first.target.click();
                    return true;
                }"""
            )
            if clicked:
                return True
        except Exception:
            pass
        _wait_short(page, 400)
    return False


def _pick_team_profile_native(page, tag, timeout=20000):
    """选 profile 弹窗：读出业务(非个人)资料名，用 Playwright 原生点击（对 React 才有效，
    合成 .click() 常点了没反应）。业务卡排在 Personal 之上，取最靠上的候选名原生点击。"""
    bad = re.compile(
        r"personal profile|个人资料|个人|learn more about profiles|sign in to a different|"
        r"^email address$|copyright|terms of use|privacy|cookie preferences|do not sell|"
        r"select a profile|choose a profile|选择.*配置文件|^adobe$",
        re.I,
    )
    deadline = time.time() + max(timeout, 0) / 1000
    while time.time() < deadline:
        try:
            names = page.evaluate(
                """() => {
                  const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
                  const vis = (n) => { const b=n.getBoundingClientRect(); const s=getComputedStyle(n);
                    return b.width>150 && b.height>=44 && b.height<=170 && s.display!=='none'
                      && s.visibility!=='hidden' && Number(s.opacity||1)>0.05; };
                  const out=[];
                  for (const n of document.querySelectorAll('a,button,[role="button"],[role="link"],[tabindex]')) {
                    if (!vis(n)) continue;
                    const t = norm(n.innerText || n.textContent || '');
                    if (!t || t.length>50) continue;
                    if (!/[a-zA-Z\\u4e00-\\u9fa5]/.test(t)) continue;
                    out.push({ t, top: n.getBoundingClientRect().top });
                  }
                  out.sort((a,b) => a.top - b.top);
                  const seen = new Set(); const res = [];
                  for (const it of out) { if (!seen.has(it.t)) { seen.add(it.t); res.push(it.t); } }
                  return res;
                }"""
            ) or []
        except Exception:
            names = []
        for name in names:
            if bad.search(name):
                continue
            try:
                page.get_by_text(name, exact=True).first.click(timeout=4000)
                print(f"[{tag}] picked team profile (native): {name!r}", flush=True)
                return True
            except Exception:
                try:
                    page.get_by_text(name).first.click(timeout=3000)
                    print(f"[{tag}] picked team profile (native loose): {name!r}", flush=True)
                    return True
                except Exception:
                    continue
        time.sleep(0.4)
    return False


def _click_business_profile_option(page, profile_name="", timeout=8000):
    """在 "选择一个配置文件" 界面挑【企业/团队】资料而不是个人资料。

    被加进团队的小号登录后会看到 Personal Profile + 业务(公司/团队)Profile 两张卡，
    团队的 4000 积分权益挂在业务 Profile 上，所以必须避开个人 Profile。
    排序优先级：显式配置名命中 > 含 business/企业 等关键词 > 非个人卡 > 靠上。
    只有当所有卡片都判定为个人时才返回 False，交回上层兜底。
    """
    deadline = time.time() + max(timeout, 0) / 1000
    wanted_names = [str(item).strip() for item in (profile_name, "weifengxinmeiti") if str(item or "").strip()]
    while time.time() < deadline:
        try:
            result = page.evaluate(
                """({ wantedNames }) => {
                    const normalize = (value) => String(value || '')
                      .replace(/[\\u200B-\\u200D\\uFEFF]/g, '')
                      .replace(/\\s+/g, ' ')
                      .trim();
                    const textOf = (node) => normalize(
                      node?.innerText
                      || node?.textContent
                      || node?.getAttribute?.('aria-label')
                      || node?.getAttribute?.('title')
                      || ''
                    );
                    const visible = (node) => {
                      if (!node || !node.getBoundingClientRect) return false;
                      const box = node.getBoundingClientRect();
                      const style = getComputedStyle(node);
                      return box.width > 0 && box.height > 0
                        && box.right > 0 && box.bottom > 0
                        && box.left < innerWidth && box.top < innerHeight
                        && style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && Number(style.opacity || 1) > 0.05;
                    };
                    const buttonish = (node) => {
                      const tag = String(node.tagName || '').toLowerCase();
                      const role = String(node.getAttribute?.('role') || '').toLowerCase();
                      const cursor = getComputedStyle(node).cursor;
                      return tag === 'button' || tag === 'a' || role === 'button'
                        || role === 'link' || node.tabIndex >= 0 || cursor === 'pointer';
                    };
                    const parentOf = (node) => {
                      if (!node) return null;
                      if (node.parentElement) return node.parentElement;
                      const root = node.getRootNode?.();
                      return root && root.host ? root.host : null;
                    };
                    const clickableAncestor = (node) => {
                      for (let cur = node, depth = 0; cur && depth < 8; cur = parentOf(cur), depth += 1) {
                        if (!visible(cur)) continue;
                        if (buttonish(cur)) return cur;
                      }
                      return null;
                    };
                    const excluded = [
                      /select a profile/i, /choose a profile/i, /选择.*配置文件/i,
                      /learn more about profiles/i, /sign in to a different account/i,
                      /^email address$/i, /copyright/i, /terms of use/i,
                      /cookie preferences/i, /privacy/i, /do not sell/i,
                    ];
                    const personalRe = /personal profile|personal account|个人资料|个人配置文件|个人账户|个人帐户|个人账号/i;
                    const businessRe = /business|enterprise|company|teams?|organi[sz]ation|work|school|企业|公司|团队|商业|工作|学校/i;
                    const wanted = (wantedNames || []).map((s) => s.toLowerCase()).filter(Boolean);
                    const emailLike = /^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/i;
                    const seen = new Set();
                    const cards = [];
                    for (const node of Array.from(document.querySelectorAll('*'))) {
                      if (!visible(node)) continue;
                      const text = textOf(node);
                      if (!text || text.length > 160) continue;
                      if (excluded.some((rule) => rule.test(text))) continue;
                      if (emailLike.test(text)) continue;
                      const target = clickableAncestor(node);
                      if (!target || !visible(target)) continue;
                      const box = target.getBoundingClientRect();
                      if (box.width < Math.min(innerWidth * 0.6, 260) || box.height < 56) continue;
                      if (box.top < 120 || box.bottom > innerHeight - 70) continue;
                      const key = `${Math.round(box.left)}:${Math.round(box.top)}:${Math.round(box.width)}:${Math.round(box.height)}`;
                      if (seen.has(key)) continue;
                      seen.add(key);
                      const lower = text.toLowerCase();
                      let score = 0;
                      if (wanted.some((w) => lower.includes(w))) score += 100;
                      if (businessRe.test(text)) score += 40;
                      if (personalRe.test(text)) score -= 100;
                      cards.push({ target, text, score, top: box.top, left: box.left, area: box.width * box.height });
                    }
                    if (!cards.length) return { ok: false, reason: 'no-card' };
                    cards.sort((a, b) => b.score - a.score || a.top - b.top || a.left - b.left || b.area - a.area);
                    const best = cards[0];
                    if (best.score < 0) return { ok: false, reason: 'only-personal' };
                    best.target.scrollIntoView({ block: 'center', inline: 'center' });
                    const box = best.target.getBoundingClientRect();
                    const x = Math.min(Math.max(box.left + box.width / 2, 8), innerWidth - 8);
                    const y = Math.min(Math.max(box.top + box.height / 2, 8), innerHeight - 8);
                    for (const name of ['pointerover','mouseover','pointerdown','mousedown','pointerup','mouseup','click']) {
                      best.target.dispatchEvent(new MouseEvent(name, { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y, button: 0 }));
                    }
                    if (typeof best.target.click === 'function') best.target.click();
                    return { ok: true, text: best.text, score: best.score };
                }""",
                {"wantedNames": wanted_names},
            )
            if result and result.get("ok"):
                print(f"[Profile] picked business profile: {result.get('text')!r} (score={result.get('score')})", flush=True)
                return True
        except Exception:
            pass
        _wait_short(page, 400)
    return False


def _submit_adobe_password(page, tag=""):
    tried = []

    if firefly._press_continue(page):
        tried.append("continue")
        _wait_short(page, 1200)
        if not firefly._visible_password_input_present(page):
            return True

    selectors = [
        "#PasswordPage-PasswordField",
        "input[type='password']",
        "input[name='password']",
        "input[id*='password' i]",
        "input[autocomplete='current-password']",
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() <= 0:
                continue
            loc.press("Enter", timeout=1500)
            tried.append(f"enter:{selector}")
            _wait_short(page, 1200)
            if not firefly._visible_password_input_present(page):
                return True
        except Exception:
            continue

    try:
        page.keyboard.press("Enter")
        tried.append("keyboard-enter")
        _wait_short(page, 1200)
        if not firefly._visible_password_input_present(page):
            return True
    except Exception:
        pass

    for selector in ["button[type='submit']", "input[type='submit']"]:
        try:
            loc = page.locator(selector).first
            if loc.count() <= 0:
                continue
            loc.click(timeout=1500, force=True)
            tried.append(f"submit:{selector}")
            _wait_short(page, 1200)
            if not firefly._visible_password_input_present(page):
                return True
        except Exception:
            continue

    if tag:
        print(f"[{tag}] password submit fallbacks tried: {', '.join(tried) or 'none'}", flush=True)
    return bool(tried)


def _complete_firefly_onboarding(page, context, tag, timeout=180, profile_name=DEFAULT_PROFILE_NAME):
    deadline = time.time() + max(timeout, 0)
    start = time.time()
    saw_onboarding = False
    last_onboarding_seen_at = 0
    last_notice = 0
    while time.time() < deadline:
        ready_page = firefly._find_firefly_session_page(context, preferred_page=page)
        if ready_page is not None and firefly._firefly_ready_for_cookie_export(ready_page):
            return ready_page

        pages = _context_pages(context, preferred_page=page)
        handled = False
        visible_onboarding = False
        for item in pages:
            text = _page_text(item).lower()
            try:
                iurl = (item.url or "").lower()
            except Exception:
                iurl = ""
            is_chooser_text = ("选择一个配置文件" in text or "choose a profile" in text or "select a profile" in text)
            is_auth_popup = "auth.services.adobe.com" in iurl
            # 选 profile 弹窗常在 auth.services.adobe.com，正文一时读不到也不能跳过
            if not text and not is_auth_popup:
                continue
            if _page_needs_firefly_onboarding(item):
                saw_onboarding = True
                visible_onboarding = True
                last_onboarding_seen_at = time.time()
            try:
                item.bring_to_front()
            except Exception:
                pass

            if is_chooser_text:
                print(f"[{tag}] selecting business/enterprise Adobe profile", flush=True)
                handled = (
                    _pick_team_profile_native(item, tag, timeout=20000)
                    or _click_business_profile_option(item, profile_name=profile_name, timeout=12000)
                    or _click_visible_text(item, [profile_name, "weifengxinmeiti"], timeout=10000)
                    or _click_first_profile_option(item, timeout=10000)
                )
            elif is_auth_popup and not text:
                # 弹窗文字读不到时也试原生选企业卡（无卡片则返回 False，安全不误点）
                handled = (
                    _pick_team_profile_native(item, tag, timeout=12000)
                    or _click_business_profile_option(item, profile_name=profile_name, timeout=8000)
                )
                if handled:
                    print(f"[{tag}] selected business profile on auth popup (text was unreadable)", flush=True)
            elif "确认您的存储选择" in text or "confirm your storage" in text:
                print(f"[{tag}] confirming personal cloud storage choice", flush=True)
                handled = _click_action(
                    item,
                    [r"^\s*Confirm\s*$", r"^\s*确认\s*$", r"^\s*Continue\s*$", r"^\s*继续\s*$"],
                    timeout=12000,
                )
            elif "是否要移动文件" in text or "move files" in text or "move your files" in text or "would you like to move" in text:
                print(f"[{tag}] keeping files in personal cloud storage", flush=True)
                handled = _click_keep_personal_cloud_storage(item, timeout=12000)
            elif "加入团队" in text or "join team" in text:
                print(f"[{tag}] joining Adobe team", flush=True)
                # 原生点击优先（React 页合成点击常无反应）；Join team 不行就 Skip for now 兜底
                handled = (
                    _click_role_button(item, ["Join team", "加入团队"], timeout=8000)
                    or _click_role_button(item, ["Skip for now", "暂不", "Not now", "稍后"], timeout=4000)
                    or _click_action(item, [r"^\s*Join team\s*$", r"^\s*加入团队\s*$",
                                            r"^\s*Skip for now\s*$"], timeout=8000)
                )
            elif "欢迎使用" in text or "welcome to" in text:
                handled = _click_action(
                    item,
                    [
                        r"^\s*Continue\s*$",
                        r"^\s*继续\s*$",
                        r"^\s*Next\s*$",
                        r"^\s*下一步\s*$",
                        r"^\s*Get started\s*$",
                        r"^\s*Done\s*$",
                        r"^\s*OK\s*$",
                    ],
                    timeout=10000,
                )

            if handled:
                _wait_short(item, 2500)
                page = item
                break

        if not handled:
            if saw_onboarding and not visible_onboarding and last_onboarding_seen_at and time.time() - last_onboarding_seen_at > 8:
                session_page = firefly._find_firefly_session_page(context, preferred_page=page)
                if session_page is not None:
                    print(f"[{tag}] onboarding prompts disappeared; continuing with Firefly session page", flush=True)
                    return session_page
                print(f"[{tag}] onboarding prompts disappeared; continuing with current page", flush=True)
                return page
            if not saw_onboarding and time.time() - start > 6:
                return firefly._find_firefly_session_page(context, preferred_page=page) or page
            now = time.time()
            if now - last_notice >= 20:
                print(f"[{tag}] waiting for Firefly team/storage/profile onboarding to finish", flush=True)
                last_notice = now
            _wait_short(page, 400)
    return firefly._find_firefly_session_page(context, preferred_page=page) or page


def _click_adobe_not_now_if_present(page, context, tag, timeout=5000):
    deadline = time.time() + max(timeout, 0) / 1000
    markers = [
        "progressive-profile/add-secondary-email",
        "progressive-profile/add-phone",
        "progressive-profile/passkey-enroll",
        "secondary email",
        "backup email",
        "recovery email",
        "phone number",
        "add mobile",
        "add a phone",
        "passkey",
        "pass key",
        "备用邮箱",
        "备用电子邮件",
        "添加备用",
        "备用手机",
        "电话号码",
        "通行密钥",
        "密钥",
    ]
    buttons = [
        "Not now",
        "Skip",
        "Maybe later",
        "暂不添加",
        "不要现在",
        "暂不",
        "跳过",
        "稍后再说",
    ]
    while time.time() < deadline:
        for item in _context_pages(context, preferred_page=page):
            try:
                url = (item.url or "").lower()
            except Exception:
                url = ""
            text = _page_text(item)
            lower = text.lower()
            if not any(marker.lower() in url or marker.lower() in lower for marker in markers):
                continue
            print(f"[{tag}] skipping Adobe secondary email/phone prompt", flush=True)
            for label in buttons:
                try:
                    item.get_by_role("button", name=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)).last.click(timeout=1200)
                    _wait_short(item, 1500)
                    return item
                except Exception:
                    pass
            if _click_visible_text(item, buttons, timeout=2500, button_only=True):
                _wait_short(item, 1500)
                return item
            if _click_visible_text(item, buttons, timeout=2500, exact=True):
                _wait_short(item, 1500)
                return item
        _wait_short(page, 500)
    return None


def _random_adobe_password():
    """生成随机强密码(避免再被 Adobe 判通用密码强制改密)。"""
    import random
    import string
    pw = [random.choice(string.ascii_uppercase), random.choice(string.ascii_lowercase),
          random.choice(string.digits), random.choice("!@#$%^&*")]
    pw += [random.choice(string.ascii_letters + string.digits) for _ in range(9)]
    random.shuffle(pw)
    return "".join(pw)


def _page_is_change_password(page):
    return _page_has_any_text(page, [
        "change your adobe password", "choose a new password", "create a new password",
        "set a new password", "you need to choose a new password",
        "更改您的 adobe 密码", "更改你的 adobe 密码", "选择新密码", "设置新密码", "创建新密码", "设置一个新密码",
    ])


def _fill_new_password(page, new_pwd):
    """把所有可见 password 输入框填成 new_pwd(改密页通常 1~2 个:新密码/确认)。"""
    try:
        n = page.evaluate(
            """(pwd) => {
                const vis=n=>{const b=n.getBoundingClientRect();const s=getComputedStyle(n);
                    return b.width>0&&b.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&!n.disabled;};
                const inps=[...document.querySelectorAll('input[type=password]')].filter(vis);
                let c=0;
                for(const i of inps){ i.focus();
                    const setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
                    setter.call(i,pwd);
                    i.dispatchEvent(new Event('input',{bubbles:true}));
                    i.dispatchEvent(new Event('change',{bubbles:true})); c++; }
                return c;
            }""",
            new_pwd,
        )
        return bool(n and n > 0)
    except Exception:
        return False


def _handle_change_password(page, context, account, proxy, tag, timeout, manual_timeout, auto_email, manual):
    """自动处理 Adobe 强制改密:Continue → 接码验证身份 → 设随机新密码 → 记录 → 继续。"""
    print(f"[{tag}] 检测到强制改密弹窗，自动处理(接码→设随机新密码)", flush=True)
    _click_action(page, [r"^\s*Continue\s*$", r"^\s*继续\s*$", r"^\s*Next\s*$", r"^\s*下一步\s*$"], timeout=6000)
    _wait_short(page, 2500)
    # 验证身份(邮箱接码) — 复用现有验证(refresh_token 走邮箱池)
    if firefly._page_requires_adobe_email_verification(page) or _find_best_auth_page(context, preferred_page=page) is not None:
        vp = _complete_login_verification(page, context, account, proxy, tag, timeout, manual_timeout, auto_email, manual)
        if vp is not None:
            page = vp
        _wait_short(page, 2000)
    # 设新密码
    new_pwd = _random_adobe_password()
    deadline = time.time() + max(timeout, 60)
    done = False
    while time.time() < deadline:
        if firefly._page_requires_adobe_email_verification(page):
            vp = _complete_login_verification(page, context, account, proxy, tag, timeout, manual_timeout, auto_email, manual)
            if vp is not None:
                page = vp
            _wait_short(page, 1500)
            continue
        if firefly._visible_password_input_present(page) or _page_has_any_text(page, ["new password", "新密码", "create a new password", "设置新密码"]):
            if _fill_new_password(page, new_pwd):
                try:
                    page.keyboard.press("Enter")
                except Exception:
                    pass
                _click_action(page, [r"^\s*Continue\s*$", r"^\s*Save\s*$", r"^\s*Reset\s*$", r"^\s*Submit\s*$",
                                     r"^\s*Done\s*$", r"^\s*继续\s*$", r"^\s*保存\s*$", r"^\s*完成\s*$"], timeout=4000)
                done = True
                break
        _wait_short(page, 1200)
    if done:
        try:
            append_line_locked(NEWPASS_FILE, f"{account['email']}----{new_pwd}")
        except Exception:
            pass
        account["password"] = new_pwd
        print(f"[{tag}] ✅ 改密完成，新密码已记入 team_seats_newpass.txt", flush=True)
        _wait_short(page, 2500)
    else:
        print(f"[{tag}] ⚠️ 改密未走完(新密码页结构可能不同，把那页发我调)", flush=True)
    return page


def _select_profile_if_present(page, context, tag):
    """团队号登录后会弹『Select a profile』(企业+个人两卡，AccessTokenFlow 弹窗，不选就拿不到 token 卡死)。
    实测：卡片在 auth 页【主frame】，get_by_text(exact).click() 能点掉。按 URL(profile-chooser) + 文本双检测。"""
    for pg in _context_pages(context, preferred_page=page):
        try:
            url = (pg.url or "").lower()
        except Exception:
            continue
        is_chooser_url = "profile-chooser" in url
        if "auth.services.adobe.com" not in url and not is_chooser_url:
            continue
        txt = _page_text(pg).lower()
        if not (is_chooser_url or "select a profile" in txt or "choose a profile" in txt or "选择一个配置文件" in txt):
            continue
        try:
            pg.bring_to_front()
        except Exception:
            pass
        print(f"[{tag}] profile chooser detected -> picking business profile", flush=True)
        if (_pick_team_profile_native(pg, tag, timeout=15000)
                or _click_business_profile_option(pg, timeout=8000)
                or _click_first_profile_option(pg, timeout=8000)):
            return True
    return False


def _drain_profile_chooser(page, context, tag, rounds=24):
    """反复处理 profile chooser 直到它消失（AccessTokenFlow 弹窗必须点掉才能拿到 token）。"""
    handled_any = False
    for _ in range(rounds):
        if not _select_profile_if_present(page, context, tag):
            break
        handled_any = True
        _wait_short(page, 1500)
    return handled_any


def _drain_auth_onboarding(page, context, tag, rounds=20):
    """排空所有 auth.services.adobe.com 弹窗：选profile / Welcome-Join team / 各种 onboarding。
    不依赖能否读到文字——挨个原生点击(选企业卡 → Join team → Continue/Skip)，点掉一个就再扫一轮。"""
    # 存储页：必须选"保留在个人存储"，避开"Move all files→内容迁移"(迁移会卡住、会话cookie不齐)
    KEEP = ["Keep files in personal cloud storage", "Keep files in your personal cloud storage",
            "Keep my files in personal", "Keep files separate", "保留在个人云存储", "保留文件在个人存储",
            "Don't move my files", "Keep in personal storage"]
    # affirmative 推进按钮：Join team / Confirm / Continue / 跳过 等；不点 Go back/取消/Move all
    JOIN = ["Join team", "加入团队", "Confirm", "确认", "Continue", "继续", "Next", "下一步",
            "Agree", "Accept", "同意", "Get started", "Done", "完成", "OK",
            "Skip for now", "Not now", "暂不", "稍后", "Maybe later"]
    for _ in range(rounds):
        acted = False
        stuck = None
        for pg in _context_pages(context, preferred_page=page):
            try:
                url = (pg.url or "").lower()
            except Exception:
                continue
            if "auth.services.adobe.com" not in url and "profile-chooser" not in url:
                continue
            stuck = pg
            try:
                pg.bring_to_front()
            except Exception:
                pass
            _dh = ""
            try:
                _dh = pg.evaluate("()=>document.body?document.body.innerText.replace(/\\s+/g,' ').slice(0,90):''") or ""
            except Exception:
                pass
            low = _dh.lower()
            # 登录类页面(密码/验证码)不归 onboarding 管，跳过，避免误点 Continue 死循环
            try:
                if (firefly._visible_password_input_present(pg)
                        or "enter your password" in low or "verification code" in low
                        or "incorrect password" in low or "enter the code" in low):
                    continue
            except Exception:
                pass
            is_chooser = ("profile-chooser" in url) or ("select a profile" in low)
            # 顺序：JOIN(Join team/Confirm/Continue)优先 → 再 KEEP(move页选"保留个人存储") → 最后(仅chooser页)选企业卡。
            # confirm 页有 Confirm，JOIN 先点掉不会轮到 KEEP 误点说明文字死循环；move 页没任何 JOIN 按钮，落到 KEEP 点 Keep。
            if (_click_role_button(pg, JOIN, timeout=2500)
                    or _click_text_any(pg, KEEP, timeout=2500)
                    or (is_chooser and _pick_team_profile_native(pg, tag, timeout=2500))):
                acted = True
                _wait_short(pg, 1200)
                break
        if not acted:
            if stuck is not None:
                head = ""
                try:
                    btns = stuck.evaluate(
                        "()=>[...document.querySelectorAll('button,[role=button],a')]"
                        ".filter(b=>{const r=b.getBoundingClientRect();return r.width>4&&r.height>4;})"
                        ".map(b=>(b.innerText||b.getAttribute('aria-label')||'').replace(/\\s+/g,' ').trim())"
                        ".filter(Boolean).slice(0,25)"
                    )
                    head = stuck.evaluate("()=>document.body?document.body.innerText.replace(/\\s+/g,' ').slice(0,160):''")
                    print(f"[{tag}] ⚠️ auth弹窗未识别({(stuck.url or '')[:60]})；标题: {head!r}；可见按钮: {btns}", flush=True)
                except Exception:
                    pass
                # "内容迁移中/处理中"等待页：没按钮，等它自己跳转，别立刻退出
                low = head.lower()
                if any(k in low for k in ("migrat", "迁移", "being moved", "in progress", "please wait", "正在", "处理中", "一会")):
                    print(f"[{tag}] 等待页(迁移/处理中)，等待自动跳转…", flush=True)
                    _wait_short(stuck, 4000)
                    continue
            break
    return True


_INCORRECT_PW_TEXTS = [
    "incorrect password",
    "that's an incorrect password",
    "that’s an incorrect password",
    "password is incorrect",
    "密码不正确",
    "密码错误",
    "密码有误",
]


def _finish_adobe_login_stages(page, context, account, proxy, tag, timeout, manual_timeout, auto_email, manual):
    password = account.get("password") or ""
    password_alt = account.get("password_alt") or ""
    pw_state = {"used_alt": False}

    def _enter_password(pg):
        # 返回: "filled" | "password_field_missing" | "wrong_password"
        # 页面已报"密码不正确" → 先换备列(col3)再试一次；主/备都被拒就判定密码错、立刻跳过该号（不再死填 15 次）。
        if _page_has_any_text(pg, _INCORRECT_PW_TEXTS):
            if password_alt and password_alt != password and not pw_state["used_alt"]:
                pw_state["used_alt"] = True
                print(f"[{tag}] 密码被拒，改用备列(col3)密码重试一次", flush=True)
            else:
                print(f"[{tag}] 密码错误（主/备列都不对），跳过该号", flush=True)
                return "wrong_password"
        pwd = password_alt if pw_state["used_alt"] else password
        if not firefly._fill_adobe_signin_password(pg, pwd, timeout=15000):
            return "password_field_missing"
        _submit_adobe_password(pg, tag=tag)
        return "filled"

    last_state = "unknown"
    for _ in range(15):
        skipped_page = _click_adobe_not_now_if_present(page, context, tag, timeout=1200)
        if skipped_page is not None:
            page = skipped_page
            continue
        if _page_is_change_password(page):
            page = _handle_change_password(page, context, account, proxy, tag, timeout, manual_timeout, auto_email, manual)
            continue
        if _page_needs_firefly_onboarding(page):
            return "logged_in", page
        # 登录后团队号常弹"选择 profile"(企业+个人)，独立窗口 → 立刻选企业卡，否则死等卡死
        if _select_profile_if_present(page, context, tag):
            _wait_short(page, 1500)
            continue
        # 快速直检：控件已出现就立刻处理，避免空等状态机（"出现即输入"）
        if firefly._visible_password_input_present(page):
            print(f"[{tag}] entering Adobe password", flush=True)
            st = _enter_password(page)
            if st != "filled":
                return st, page
            _wait_short(page, 1000)
            continue
        last_state = firefly._wait_for_adobe_login_state(page, timeout=10000)
        if last_state == "verification" or firefly._page_requires_adobe_email_verification(page):
            page = _complete_login_verification(
                page,
                context,
                account,
                proxy,
                tag,
                timeout,
                manual_timeout,
                auto_email,
                manual,
            )
            if page is None:
                return "verification_failed", None
            continue
        if last_state == "password" or firefly._visible_password_input_present(page):
            print(f"[{tag}] entering Adobe password", flush=True)
            st = _enter_password(page)
            if st != "filled":
                return st, page
            _wait_short(page, 1500)
            continue
        if last_state in ("logged_in", "not_found", "error", "existing_account"):
            return last_state, page
        session_page = firefly._find_firefly_session_page(context, preferred_page=page)
        if session_page is not None and firefly._firefly_ready_for_cookie_export(session_page):
            return "logged_in", session_page
        if _find_best_auth_page(context, preferred_page=page) is None and session_page is not None:
            return "logged_in", session_page
        _wait_short(page, 1000)
    return last_state, page


def _login_adobe_via_firefly_popup(page, context, account, proxy, tag, timeout, manual_timeout, auto_email, manual):
    auth_page = _open_firefly_login_popup(page, context, tag)
    if auth_page is None:
        return "popup_missing", page

    print(f"[{tag}] filling Adobe login email from mail pool: {account['email']}", flush=True)
    if not firefly._fill_adobe_signin_email(auth_page, account["email"], timeout=15000):
        return "email_field_missing", auth_page
    firefly._press_continue(auth_page)
    _wait_short(auth_page, 1200)
    return _finish_adobe_login_stages(
        auth_page,
        context,
        account,
        proxy,
        tag,
        timeout,
        manual_timeout,
        auto_email,
        manual,
    )


def _login_adobe(page, email, password, timeout):
    signin_urls = [
        "https://account.adobe.com/#/signin",
        "https://account.adobe.com/#/",
        "https://auth.services.adobe.com/en_US/deeplink.html#/signin",
    ]

    last_state = "unknown"
    for url in signin_urls:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        _wait_short(page, 1500)
        last_state = firefly._wait_for_adobe_login_state(page, timeout=8000)
        if last_state in ("logged_in", "verification", "password"):
            break
        if firefly._fill_adobe_signin_email(page, email, timeout=10000):
            firefly._press_continue(page)
            last_state = firefly._wait_for_adobe_login_state(page, timeout=30000)
            break

    if last_state == "logged_in":
        return last_state
    if last_state == "verification":
        return last_state
    if last_state == "not_found":
        return last_state
    if last_state != "password" and not firefly._visible_password_input_present(page):
        return last_state

    if not firefly._fill_adobe_signin_password(page, password, timeout=12000):
        return "password_field_missing"
    _submit_adobe_password(page, tag="direct-login")
    return firefly._wait_for_adobe_login_state(page, timeout=timeout * 1000)


def _complete_verification(page, context, account, proxy, tag, timeout, manual_timeout, auto_email, manual):
    verification_state = {"done": False, "saw_page": firefly._page_requires_adobe_email_verification(page)}
    if auto_email:
        reg, mail_token, outlook_refresh_token, outlook_client_id, can_poll = _make_mail_context(account, proxy, tag)
        if can_poll:
            if firefly._handle_firefly_email_verification(
                page,
                context,
                reg,
                mail_token,
                outlook_refresh_token=outlook_refresh_token,
                outlook_client_id=outlook_client_id,
                timeout=max(timeout, 60),
                detection_timeout=3,
                verification_state=verification_state,
                require_verification=True,
            ):
                session_page = firefly._find_firefly_session_page(context, preferred_page=page)
                return session_page or page
        else:
            print(f"[{tag}] no usable mailbox access for automatic Adobe verification", flush=True)

    if manual and manual_timeout > 0:
        result = firefly._wait_for_manual_email_verification_completion(
            page,
            context,
            verification_state,
            timeout=manual_timeout * 1000,
        )
        if result is not None:
            return result
    return None


_REAUTH_HOSTS = ("auth.services.adobe.com", "adobelogin.com")


def _find_reauth_page(context, preferred_page=None):
    """登录成功后 Firefly 会触发 reauth(reauthenticate=check/force)，把某个 tab 停在
    auth.services 的 Sign in 页（标题就叫 Sign in）。_firefly_ready 要求 url 含 firefly.adobe.com，
    停在 auth 页就永远 not ready → 60s 超时 → "Cookie export failed"。这里找出这种 auth 页。"""
    for pg in _context_pages(context, preferred_page=preferred_page):
        try:
            u = (pg.url or "").lower()
        except Exception:
            continue
        if any(h in u for h in _REAUTH_HOSTS) and "firefly.adobe.com" not in u:
            return pg
    return None


def _dump_auth_page(context, tag, label):
    """诊断：把当前所有页面的 url/标题/可见按钮/输入框 dump 出来 + 截图，定位卡点。"""
    for pg in _context_pages(context):
        try:
            u = pg.url or ""
        except Exception:
            continue
        try:
            ttl = pg.title()
        except Exception:
            ttl = "?"
        try:
            btns = pg.evaluate(
                "()=>[...document.querySelectorAll('button,[role=button],a,input[type=submit]')]"
                ".filter(b=>{const r=b.getBoundingClientRect();return r.width>4&&r.height>4;})"
                ".map(b=>(b.innerText||b.value||b.getAttribute('aria-label')||'').replace(/\\s+/g,' ').trim())"
                ".filter(Boolean).slice(0,25)")
        except Exception as exc:
            btns = f"err:{exc}"
        try:
            ins = pg.evaluate(
                "()=>[...document.querySelectorAll('input')]"
                ".filter(i=>{const r=i.getBoundingClientRect();return r.width>2&&r.height>2;})"
                ".map(i=>i.type+'/'+(i.name||i.id||i.placeholder||i.getAttribute('aria-label')||''))")
        except Exception as exc:
            ins = f"err:{exc}"
        print(f"[{tag}] [DUMP {label}] url={u[:90]} title={ttl!r}", flush=True)
        print(f"[{tag}] [DUMP {label}] inputs={ins}", flush=True)
        print(f"[{tag}] [DUMP {label}] buttons={btns}", flush=True)
        try:
            pg.screenshot(path=os.path.join("firefly_debug", f"diag_{tag}_{label}.png"))
        except Exception:
            pass


def _complete_reauth_signin(page, context, account, proxy, tag, timeout, manual_timeout, auto_email, manual):
    """走完 reauth 重新登录页：check 模式常只需点"继续/选已登录账号"一步；force 模式要重输密码。
    点完/输完后回到 firefly 主站。返回应继续使用的 page。"""
    auth = _find_reauth_page(context, preferred_page=page)
    if auth is None:
        return page
    try:
        cururl = (auth.url or "")[:90]
    except Exception:
        cururl = "?"
    print(f"[{tag}] [REAUTH] 检测到 reauth 重新登录页，走完它: {cururl}", flush=True)
    try:
        auth.bring_to_front()
    except Exception:
        pass
    _wait_short(auth, 700)
    # check-reauth 多是"继续/选择已登录账号"一步：先点掉（没有密码框时才点，避免误触）
    if not firefly._visible_password_input_present(auth):
        _click_role_button(auth, ["Continue", "继续", "Sign in", "Yes", "Confirm"], timeout=900)
        _click_text_any(auth, [account["email"]], timeout=900)
        _wait_short(auth, 900)
    # 若要求重填邮箱：填邮箱→Continue
    try:
        if firefly._fill_adobe_signin_email(auth, account["email"], timeout=2000):
            firefly._press_continue(auth)
            _wait_short(auth, 1200)
    except Exception:
        pass
    # 密码/接码/选 profile 状态机走完（成功后会跳回 firefly，状态机检测到会话页即返回）
    st, p = _finish_adobe_login_stages(
        auth, context, account, proxy, tag, timeout, manual_timeout, auto_email, manual
    )
    print(f"[{tag}] [REAUTH] 走完 state={st}", flush=True)
    sess = firefly._find_firefly_session_page(context, preferred_page=p or auth)
    return sess or p or page


def _open_firefly_and_export(page, context, account, proxy, tag, timeout, manual_timeout, auto_email, manual):
    # 登录成功后会立刻弹 profile chooser / Welcome-Join team（AccessTokenFlow）。必须先点掉，
    # 否则主页拿不到 token，后面会误进邮箱验证/手动等待分支卡死。原生点击挨个放过去。
    _te = time.time()
    _drain_auth_onboarding(page, context, tag)
    print(f"[{tag}] ⏱⏱ drain1 +{time.time()-_te:.0f}s", flush=True)
    page = _complete_firefly_onboarding(
        page,
        context,
        tag,
        timeout=max(timeout, 180),
        profile_name=DEFAULT_PROFILE_NAME,
    )
    print(f"[{tag}] ⏱⏱ drain1+complete +{time.time()-_te:.0f}s", flush=True)
    # 已经在 firefly 主站就别再 goto 重载（登录+选profile后已在主页，重载就是你看到的"刷新一下"，省 5-10s）
    cur = ""
    try:
        cur = (page.url or "").lower()
    except Exception:
        pass
    if "firefly.adobe.com" not in cur:
        try:
            page.goto("https://firefly.adobe.com/", wait_until="commit", timeout=25000)
        except Exception as exc:
            print(f"[{tag}] Firefly open failed: {exc}", flush=True)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
    _wait_short(page, 1000)
    firefly._dismiss_firefly_popups(page)
    # auth 弹窗(选profile / Join team)可能(再)出现，点掉它才能拿到 token
    _drain_auth_onboarding(page, context, tag)

    # 选完 profile 后 IMS token 几秒内才到达；先轮询等就绪（最多 ~7s，顺手关 What's new 弹窗），
    # 就绪了就直接跳过下面的邮箱验证分支，避免那 ~10s 误探测白等。
    print(f"[{tag}] ⏱⏱ goto firefly+drain2 +{time.time()-_te:.0f}s", flush=True)
    for _ in range(10):
        if firefly._firefly_ready_for_cookie_export(page):
            break
        firefly._dismiss_firefly_popups(page)
        try:
            page.wait_for_timeout(700)
        except Exception:
            break
    print(f"[{tag}] ⏱⏱ ready轮询 +{time.time()-_te:.0f}s (ready={firefly._firefly_ready_for_cookie_export(page)})", flush=True)

    # ★reauth 修复：登录后 Firefly 跳到 auth.services 的 Sign in 页（reauth=check/force），
    # 停在 auth 域就永远 not ready。这里检测并走完 reauth 再回 firefly，最多两轮。
    for _ra in range(2):
        if firefly._firefly_ready_for_cookie_export(page):
            break
        if _find_reauth_page(context, preferred_page=page) is None:
            break
        page = _complete_reauth_signin(
            page, context, account, proxy, tag, timeout, manual_timeout, auto_email, manual
        ) or page
        try:
            _cur = (page.url or "").lower()
        except Exception:
            _cur = ""
        if "firefly.adobe.com" not in _cur:
            try:
                page.goto("https://firefly.adobe.com/", wait_until="commit", timeout=25000)
            except Exception:
                pass
            _wait_short(page, 1000)
        _drain_auth_onboarding(page, context, tag)
        for _ in range(12):
            if firefly._firefly_ready_for_cookie_export(page):
                break
            firefly._dismiss_firefly_popups(page)
            try:
                page.wait_for_timeout(700)
            except Exception:
                break
        print(f"[{tag}] ⏱⏱ reauth后 +{time.time()-_te:.0f}s (ready={firefly._firefly_ready_for_cookie_export(page)})", flush=True)

    if not firefly._firefly_ready_for_cookie_export(page):
        reg, mail_token, outlook_refresh_token, outlook_client_id, can_poll = _make_mail_context(account, proxy, tag)
        verification_state = {"done": False}
        if auto_email and can_poll and firefly._handle_firefly_email_verification(
            page,
            context,
            reg,
            mail_token,
            outlook_refresh_token=outlook_refresh_token,
            outlook_client_id=outlook_client_id,
            timeout=max(timeout, 60),
            detection_timeout=4,
            verification_state=verification_state,
            require_verification=False,
        ):
            session_page = firefly._find_firefly_session_page(context, preferred_page=page)
            if session_page is not None:
                page = session_page
            firefly._dismiss_firefly_popups(page)

    if not firefly._firefly_ready_for_cookie_export(page) and manual and manual_timeout > 0:
        manual_page = firefly._wait_for_manual_email_verification_completion(
            page,
            context,
            {"done": False},
            timeout=manual_timeout * 1000,
        )
        if manual_page is not None:
            page = manual_page
            try:
                page.goto("https://firefly.adobe.com/", wait_until="commit", timeout=25000)
            except Exception:
                pass
            _wait_short(page, 1000)
            firefly._dismiss_firefly_popups(page)

    page = _complete_firefly_onboarding(
        page,
        context,
        tag,
        timeout=max(timeout, 180),
        profile_name=DEFAULT_PROFILE_NAME,
    )
    print(f"[{tag}] ⏱⏱ 邮箱验证分支+onboarding3 +{time.time()-_te:.0f}s", flush=True)
    ready_page = firefly._wait_for_firefly_cookie_export_ready(page, context, timeout=60000)
    if ready_page is not None:
        page = ready_page
    print(f"[{tag}] ⏱⏱ wait_ready完 +{time.time()-_te:.0f}s", flush=True)

    if not firefly._firefly_ready_for_cookie_export(page):
        _dump_auth_page(context, tag, "not_ready")
        base = firefly._save_debug_artifacts(page, f"{tag}_login_extract_not_ready")
        raise RuntimeError(f"Firefly page is not ready for cookie export, debug saved: {base}.png / {base}.txt")

    if not firefly._append_adobe2api_cookie(account["email"], context, page=page):
        base = firefly._save_debug_artifacts(page, f"{tag}_login_extract_cookie_failed")
        raise RuntimeError(f"Cookie export failed, debug saved: {base}.png / {base}.txt")


def extract_one(account, idx, total, args):
    email = account["email"]
    tag = f"login-cookie-{idx}"
    if args.skip_existing and _cookie_exists(email):
        print(f"[{idx}/{total}] SKIP existing cookie: {email}", flush=True)
        _record_extracted_account(account, status="已完成提取")
        _complete_mail_pool_account(account, "cookie already exists")
        return "skip", email, None
    if not account.get("password"):
        reason = "missing Adobe account password in mail pool record"
        _mark_mail_pool_account(account, "failed", reason)
        append_line_locked(FAILED_FILE, f"{account['raw']}----ERROR: {reason}")
        print(f"[{idx}/{total}] FAIL {email}: {reason}", flush=True)
        return "fail", email, reason

    browser = None
    completed_ok = False
    try:
        print(f"[{idx}/{total}] login extract: {email}", flush=True)
        _t0 = time.time()
        acc_proxy = args.proxy
        if getattr(args, "ip_pool", False) and _proxypool is not None:
            _pp = _proxypool.pick_proxy()
            if _pp:
                acc_proxy = _pp
                print(f"[{tag}] 走IP池代理(每号换IP防软封): {_pp}", flush=True)
        with sync_playwright() as p:
            browser = firefly._launch_browser(p, proxy=acc_proxy, headless=args.headless)
            print(f"[{tag}] ⏱启浏览器 +{time.time()-_t0:.0f}s", flush=True)
            context = _new_context(browser)
            page = context.new_page()

            state, page = _login_adobe_via_firefly_popup(
                page,
                context,
                account,
                acc_proxy,
                tag,
                args.timeout,
                args.manual_timeout,
                args.auto_email,
                args.manual,
            )
            print(f"[{tag}] ⏱登录弹窗+邮箱 +{time.time()-_t0:.0f}s (state={state})", flush=True)
            if state in ("popup_missing", "email_field_missing"):
                print(f"[{idx}/{total}] Firefly popup login state={state}; falling back to direct Adobe login", flush=True)
                state = _login_adobe(page, email, account["password"], timeout=args.timeout)
            if state == "not_found":
                raise RuntimeError("Adobe account not found")
            if state not in ("logged_in", "verification"):
                current_url = getattr(page, "url", "") if page is not None else ""
                raise RuntimeError(f"Adobe login did not complete, state={state}, url={current_url}")

            if state == "verification" or firefly._page_requires_adobe_email_verification(page):
                page = _complete_login_verification(
                    page,
                    context,
                    account,
                    acc_proxy,
                    tag,
                    args.timeout,
                    args.manual_timeout,
                    args.auto_email,
                    args.manual,
                )
                if page is None:
                    raise RuntimeError("Adobe email verification was not completed")
                print(f"[{tag}] ⏱接码完 +{time.time()-_t0:.0f}s", flush=True)
                state, page = _finish_adobe_login_stages(
                    page,
                    context,
                    account,
                    acc_proxy,
                    tag,
                    args.timeout,
                    args.manual_timeout,
                    args.auto_email,
                    args.manual,
                )
                if state not in ("logged_in", "verification"):
                    current_url = getattr(page, "url", "") if page is not None else ""
                    raise RuntimeError(f"Adobe login did not complete after verification, state={state}, url={current_url}")

            print(f"[{tag}] ⏱密码+选企业完 +{time.time()-_t0:.0f}s", flush=True)
            _open_firefly_and_export(
                page,
                context,
                account,
                acc_proxy,
                tag,
                args.timeout,
                args.manual_timeout,
                args.auto_email,
                args.manual,
            )
            print(f"[{tag}] ⏱导cookie完 +{time.time()-_t0:.0f}s ★总计", flush=True)
            append_line_locked(SUCCESS_FILE, account["raw"])
            _record_extracted_account(account, status="已完成提取")
            _complete_mail_pool_account(account, "cookie exported")
            print(f"[{idx}/{total}] OK cookie exported: {email}", flush=True)
            completed_ok = True
            return "ok", email, None
    except Exception as exc:
        reason = str(exc).replace("\r", " ").replace("\n", " ")
        append_line_locked(FAILED_FILE, f"{account['raw']}----ERROR: {reason}")
        _mark_mail_pool_account(account, "failed", reason)
        print(f"[{idx}/{total}] FAIL {email}: {reason}", flush=True)
        return "fail", email, reason
    finally:
        if browser and (args.headless or completed_ok or not firefly._keep_failed_browser()):
            try:
                browser.close()
            except Exception:
                pass


def _run_pass(pending, args, workers, label=""):
    """并发处理一批账号；返回 (ok数, skip数, 失败账号列表)。主跑和重跑共用。"""
    total = len(pending)
    ok = skip = 0
    failed = []
    workers = max(1, min(workers, total))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        nxt = [0]

        def submit_next():
            if nxt[0] >= total:
                return
            i = nxt[0]
            nxt[0] += 1
            acc = pending[i]
            futures[executor.submit(extract_one, acc, i + 1, total, args)] = acc

        for _ in range(workers):
            submit_next()
        while futures:
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for fut in done:
                acc = futures.pop(fut, None)
                status, _, _ = fut.result()
                if status == "ok":
                    ok += 1
                elif status == "skip":
                    skip += 1
                else:
                    if acc is not None:
                        failed.append(acc)
                submit_next()
                print(f"[Progress{label}] ok={ok} skip={skip} fail={len(failed)} done={ok + skip + len(failed)}/{total}", flush=True)
    return ok, skip, failed


def run(args):
    accounts = _load_accounts_from_mail_pool(limit=args.limit) if args.from_mail_pool else _load_accounts(args.accounts, limit=args.limit)
    if not accounts:
        source = "Firefly mail pool" if args.from_mail_pool else args.accounts
        raise RuntimeError(f"no valid accounts found in {source}")

    if args.reset_logs:
        atomic_write_text(FAILED_FILE, "", encoding="utf-8")
        atomic_write_text(SUCCESS_FILE, "", encoding="utf-8")

    total = len(accounts)
    print("#" * 60)
    print("Adobe Firefly login cookie extractor")
    print(f"Accounts: {'Firefly mail pool' if args.from_mail_pool else args.accounts}")
    print(f"Count: {total} | Workers: {args.workers} | Headless: {args.headless}")
    print(f"Skip existing: {args.skip_existing} | Auto email: {args.auto_email} | Manual: {args.manual}")
    print(f"Flow: Firefly popup login -> newest email code -> password -> team/storage/profile -> cookie export")
    print(f"Cookie JSON: {firefly.COOKIE_JSON_FILE}")
    print("#" * 60)

    workers = max(1, min(args.workers, total))
    ok, skip, failed = _run_pass(accounts, args, workers)
    # 失败自动重跑：导cookie的失败大多是 Adobe 反爬假报(incorrect password / account not found，号其实活的)，
    # 重跑就成（实测）。轮间隔让反爬限流冷却。验活已剔真死号，这里救反爬假报。
    retry_rounds = max(0, int(getattr(args, "retry_rounds", 2) or 0))
    rno = 0
    while failed and rno < retry_rounds:
        rno += 1
        print("#" * 60, flush=True)
        print(f"#### 第 {rno}/{retry_rounds} 轮重跑 {len(failed)} 个失败号（失败大多反爬假报，重跑就成），先等 20s 让限流冷却 ####", flush=True)
        time.sleep(20)
        rok, rskip, failed = _run_pass(failed, args, workers, label=f"-retry{rno}")
        ok += rok
        skip += rskip
        print(f"#### 第 {rno} 轮重跑后：救回 {rok} 个，仍失败 {len(failed)} 个 ####", flush=True)
    fail = len(failed)

    order_result = _order_cookie_json_by_registered() if (args.refresh_missing and not args.from_mail_pool) else None
    final_cookie_count = len(_load_cookie_email_set())
    print("#" * 60)
    print(f"Done. ok={ok} skip={skip} fail={fail} cookie_unique={final_cookie_count}")
    if order_result is not None:
        print(
            f"Cookie JSON ordered by registered_accounts.txt: "
            f"ordered={order_result['ordered']} missing={order_result['missing']} dropped={order_result['dropped']}"
        )
        print(f"Missing cookie accounts refreshed: {MISSING_COOKIE_FILE} ({order_result['missing']})")
    elif args.from_mail_pool:
        print("Cookie JSON kept in extraction order because accounts came from the Firefly mail pool")
    print(f"Success log: {SUCCESS_FILE}")
    print(f"Failed log: {FAILED_FILE}")
    print("#" * 60)
    return 0 if fail == 0 else 1


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Login existing Adobe Firefly accounts and export Adobe2API cookies.")
    parser.add_argument("--accounts", default=DEFAULT_ACCOUNTS_FILE, help="account file, default: missing_cookie_accounts.txt if present")
    parser.add_argument("--from-mail-pool", action="store_true", help="take registered Adobe accounts from the Firefly mail pool")
    parser.add_argument("--workers", type=int, default=1, help="parallel browsers")
    parser.add_argument("--retry-rounds", dest="retry_rounds", type=int, default=2, help="失败号自动重跑轮数(反爬假报重跑就成,默认2,0=不重跑)")
    parser.add_argument("--ip-pool", dest="ip_pool", action="store_true", help="每号从产号IP节点池取一个干净出口IP(防Adobe per-IP软封)")
    parser.add_argument("--limit", type=int, default=0, help="only process first N accounts")
    parser.add_argument("--proxy", default="", help="browser proxy, for example http://127.0.0.1:7890")
    parser.add_argument("--headless", action="store_true", help="run browser headless")
    parser.add_argument("--timeout", type=int, default=180, help="automatic verification/login timeout seconds")
    parser.add_argument("--manual-timeout", type=int, default=600, help="manual verification wait seconds when browser is headed")
    parser.add_argument("--force", action="store_true", help="re-export even if the email already has a cookie")
    parser.add_argument("--no-auto-email", dest="auto_email", action="store_false", help="do not poll mailbox APIs for verification codes")
    parser.add_argument("--no-manual", dest="manual", action="store_false", help="do not wait for manual browser verification")
    parser.add_argument("--keep-logs", dest="reset_logs", action="store_false", help="append to previous success/failure logs")
    parser.add_argument("--no-refresh-missing", dest="refresh_missing", action="store_false", help="do not refresh missing_cookie_accounts.txt after the run")
    parser.set_defaults(auto_email=True, manual=True, reset_logs=True, refresh_missing=True)
    args = parser.parse_args(argv)
    args.accounts = os.path.abspath(args.accounts)
    args.proxy = (args.proxy or "").strip() or None
    args.workers = max(1, args.workers)
    args.skip_existing = not args.force
    if args.headless and args.manual:
        args.manual = False
    return args


if __name__ == "__main__":
    try:
        raise SystemExit(run(parse_args(sys.argv[1:])))
    except KeyboardInterrupt:
        print("stopped by user", flush=True)
        raise SystemExit(130)
