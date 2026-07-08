import os
import re
import time
import json
import random
import string
import traceback
import hashlib
import io
import sys
import html
import threading
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from network_proxy import requests_proxies
from safe_file_io import atomic_write_json, atomic_write_text, append_line_locked, exclusive_file_lock


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(BASE_DIR, "firefly_registered_accounts.txt")
ACCOUNTS_FILE = os.path.join(BASE_DIR, "registered_accounts.txt")
COOKIE_JSON_FILE = os.path.join(BASE_DIR, "firefly_adobe2api_cookies.json")
ARTIFACT_DIR = os.path.join(BASE_DIR, "firefly_debug")
os.makedirs(ARTIFACT_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
OUTLOOK_CURSOR_PATH = os.path.join(BASE_DIR, "firefly_outlook_cursor.txt")
ADOBE_ALREADY_VERIFIED = "__ADOBE_ALREADY_VERIFIED__"
_REQUESTS_PROXIES = requests_proxies()

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
















def _config_bool(name, default=False):
    cfg = _load_config()
    raw = os.environ.get(name.upper())
    if raw is None:
        raw = cfg.get(name, default)
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return bool(default)


def _config_int(name, default, minimum=None, maximum=None):
    cfg = _load_config()
    raw = os.environ.get(name.upper())
    if raw is None:
        raw = cfg.get(name, default)
    try:
        value = int(raw)
    except Exception:
        value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value




_CONFIG_CACHE = {"mtime": None, "data": {}}
_CONFIG_CACHE_LOCK = threading.Lock()

# 记录当前批次的 worker 数，邮件轮询间隔随并发数线性放宽，
# 防止 10 个 worker 同时对自托管/CF Worker 邮箱 API 形成瞬时洪峰。
_ACTIVE_WORKERS = 1
_ACTIVE_WORKERS_LOCK = threading.Lock()




def _mail_poll_interval(base_seconds):
    """邮件轮询间隔：单 worker 时压到 ~1.5s 以快速发现验证码到达；
    多 worker 时按 worker 数温和放宽，避免邮箱 API 被限速。
    可通过 firefly_mail_poll_min_seconds 覆盖最小间隔（默认 1.5s）。"""
    with _ACTIVE_WORKERS_LOCK:
        workers = _ACTIVE_WORKERS
    try:
        floor = float(_config_int("firefly_mail_poll_min_seconds", 0) or 0)
    except Exception:
        floor = 0.0
    if floor <= 0:
        floor = 1.5
    if workers <= 1:
        return floor
    factor = 1.0 + min(1.0, (workers - 1) * 0.1)
    return max(floor, base_seconds * factor * 0.6)


def _load_config():
    # 带 mtime 失效的缓存：批量并发场景下 _config_bool/_config_int 会被频繁调用，
    # 不再每次都打开 + 解析 config.json，从而显著降低磁盘 IO。
    try:
        try:
            mtime = os.path.getmtime(CONFIG_PATH)
        except OSError:
            mtime = None
        with _CONFIG_CACHE_LOCK:
            if mtime is not None and _CONFIG_CACHE["mtime"] == mtime and _CONFIG_CACHE["data"] is not None:
                return _CONFIG_CACHE["data"]
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
        with _CONFIG_CACHE_LOCK:
            _CONFIG_CACHE["mtime"] = mtime
            _CONFIG_CACHE["data"] = data
        return data
    except Exception:
        return {}










def _normalize_base_url(value, default_scheme="https"):
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    if not re.match(r"^https?://", text, re.I):
        text = f"{default_scheme}://{text}"
    return text


def _normalize_proxy_url(proxy):
    text = str(proxy or "").strip()
    if not text:
        return None
    if re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I):
        return text
    if "@" in text:
        return f"http://{text}"
    parts = text.split(":")
    if len(parts) == 4 and parts[1].isdigit():
        from urllib.parse import quote

        host, port, username, password = parts
        return f"http://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
    return f"http://{text}"


def _playwright_proxy_config(proxy):
    text = _normalize_proxy_url(proxy)
    if not text:
        return None
    from urllib.parse import unquote, urlsplit, urlunsplit

    parsed = urlsplit(text)
    if not parsed.username and not parsed.password:
        return {"server": text}
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    server = urlunsplit((parsed.scheme or "http", host, "", "", ""))
    return {
        "server": server,
        "username": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
    }


_FIREFLY_PROXY_POOL_CACHE = {"raw": None, "list": []}
_FIREFLY_PROXY_POOL_LOCK = threading.Lock()






















_VERIFY_COOKIE_NAME_RE = re.compile(r"EmailVerification|EmailCodeEntryForm|acct_evs", re.I)
_VERIFY_COOKIE_VALUE_RE = re.compile(r"EmailVerification|EmailCodeEntryForm|acct_evs", re.I)
_ADOBE_COOKIE_DOMAINS = (
    "adobe.com",
    "adobe.io",
    "adobelogin.com",
    "adobedtm.com",
    "adobeccstatic.com",
    "adobeusercontent.com",
    "adobeaemcloud.com",
)
_ADOBE_LOGIN_COOKIE_NAMES = {
    "ims_sid",
    "aux_sid",
    "idg_token",
    "fg",
    "rdc",
    "relay",
    "ftrset",
    "locale",
    "arid",
}
_ADOBE_COOKIE_DOMAIN_PRIORITY = (
    "ims-na1.adobelogin.com",
    "adobelogin.com",
    "auth.services.adobe.com",
    "account.adobe.com",
    "firefly.adobe.com",
    "adobe.com",
)


def _build_cookie_header(cookies):
    parts = []
    for item in cookies or []:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        parts.append(f"{name}={item.get('value') or ''}")
    return "; ".join(parts)
def _adobe_cookie_rank(item):
    name = str(item.get("name") or "").strip().lower()
    domain = str(item.get("domain") or "").strip().lstrip(".").lower()
    try:
        domain_rank = next(
            idx
            for idx, suffix in enumerate(_ADOBE_COOKIE_DOMAIN_PRIORITY)
            if domain == suffix or domain.endswith(f".{suffix}")
        )
    except StopIteration:
        domain_rank = len(_ADOBE_COOKIE_DOMAIN_PRIORITY)
    login_rank = 0 if name in _ADOBE_LOGIN_COOKIE_NAMES else 1
    return (login_rank, domain_rank)


def _dedupe_adobe_cookies(cookies):
    by_name = {}
    for item in sorted(cookies or [], key=_adobe_cookie_rank):
        name = str(item.get("name") or "").strip()
        if name and name not in by_name:
            by_name[name] = item
    return list(by_name.values())


def _is_email_verification_cookie(item):
    name = str(item.get("name") or "")
    value = str(item.get("value") or "")
    return bool(_VERIFY_COOKIE_NAME_RE.search(name) or _VERIFY_COOKIE_VALUE_RE.search(value))


def _is_adobe_cookie(item):
    domain = str(item.get("domain") or "").strip().lstrip(".").lower()
    return any(domain == suffix or domain.endswith(f".{suffix}") for suffix in _ADOBE_COOKIE_DOMAINS)


def _adobe2api_cookie_header_from_context(context):
    cookies = context.cookies()
    filtered_cookies = []
    skipped = []
    for item in cookies or []:
        if not _is_adobe_cookie(item):
            continue
        if _is_email_verification_cookie(item):
            skipped.append(str(item.get("name") or ""))
            continue
        filtered_cookies.append(item)
    return _build_cookie_header(_dedupe_adobe_cookies(filtered_cookies)), skipped


def _normalize_adobe2api_cookie_entries(data):
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        data = data.get("items")
    elif isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []

    entries = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        cookie = str(entry.get("cookie") or "").strip()
        if not cookie:
            continue
        name = str(entry.get("name") or entry.get("email") or "").strip()
        entries.append(
            {
                "name": name or None,
                "cookie": cookie,
            }
        )
    return entries


def _load_adobe2api_cookie_entries():
    try:
        if os.path.exists(COOKIE_JSON_FILE):
            with open(COOKIE_JSON_FILE, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        else:
            data = []
    except Exception:
        data = []

    return _normalize_adobe2api_cookie_entries(data)


def _write_adobe2api_cookie_entries(entries):
    normalized = _normalize_adobe2api_cookie_entries(entries)
    payload = {"items": normalized}
    atomic_write_json(COOKIE_JSON_FILE, payload, indent=2, ensure_ascii=False)


def _append_registered_account(email, password, email_password="", status="firefly"):
    line = f"{email}----{password}----{email_password}----{status}"
    with exclusive_file_lock(ACCOUNTS_FILE):
        existing = []
        if os.path.exists(ACCOUNTS_FILE):
            with open(ACCOUNTS_FILE, "r", encoding="utf-8-sig") as f:
                existing = [item.strip() for item in f if item.strip()]
        target = str(email or "").strip().lower()
        kept = []
        for item in existing:
            item_email = item.split("----", 1)[0].strip().lower()
            if item_email != target:
                kept.append(item)
        kept.append(line)
        atomic_write_text(ACCOUNTS_FILE, "".join(item + "\n" for item in kept), encoding="utf-8")
    print(f"[Account] registered account library updated: {ACCOUNTS_FILE}", flush=True)




def _seed_ims_session_cookies(page):
    """登录后强制 IMS reAuthenticate，把 ims_sid/aux_sid/relay 等会话 cookie 种到可读域。
    订阅版原来要求这几个 cookie 却没做握手，常导致『missing required Adobe login cookies』。"""
    if not _ims_access_token(page):
        return
    # 会话 cookie 已齐就直接走，不再握手+等待（导出前 readiness 多数已凑齐，省掉这 8s）
    try:
        names = {str(c.get("name", "")).lower() for c in page.context.cookies()}
        if {"ims_sid", "relay"} <= names:
            return
    except Exception:
        pass
    try:
        page.evaluate("()=>{try{window.adobeIMS&&window.adobeIMS.reAuthenticate&&window.adobeIMS.reAuthenticate({},'check');}catch(e){}}")
    except Exception:
        pass
    # 轮询最多 8s：ims_sid+relay 一种到就立刻走，不死等（提速）
    for _ in range(16):
        try:
            names = {str(c.get("name", "")).lower() for c in page.context.cookies()}
            if {"ims_sid", "relay"} <= names:
                return
        except Exception:
            pass
        try:
            page.wait_for_timeout(500)
        except Exception:
            return


def _append_adobe2api_cookie(email, context, page=None):
    if page is not None and _page_requires_adobe_email_verification(page):
        print("[Cookie] Adobe page still requires email verification; skip Adobe2API cookie export", flush=True)
        return False

    # 先强制 IMS 握手种会话 cookie（FF 秒导出关键步骤），再抓
    if page is not None:
        _seed_ims_session_cookies(page)

    cookie_header, skipped = _adobe2api_cookie_header_from_context(context)
    if skipped:
        print(
            f"[Cookie] removed email-verification cookies before export: {', '.join(sorted(set(skipped)))}",
            flush=True,
        )

    if not cookie_header:
        return False
    stats = _cookie_header_stats(cookie_header)
    if stats["missing"]:
        print(f"[Cookie] missing required Adobe login cookies before export: {', '.join(stats['missing'])}", flush=True)
        return False

    item = {"name": email, "cookie": cookie_header}
    with exclusive_file_lock(COOKIE_JSON_FILE):
        data = _load_adobe2api_cookie_entries()
        data = [
            {
                "name": str(entry.get("name") or "").strip() or None,
                "cookie": str(entry.get("cookie") or "").strip(),
            }
            for entry in data
            if str(entry.get("name") or "").lower() != email.lower()
            and str(entry.get("cookie") or "") != cookie_header
        ]
        data.append(item)
        _write_adobe2api_cookie_entries(data)
    print(
        f"[Cookie] adobe2api cookie JSON updated: {COOKIE_JSON_FILE} "
        f"(length={stats['length']}, count={stats['count']})",
        flush=True,
    )
    return True


def _mail_auth_headers(token="", admin_password="", prefer_bearer=True):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if admin_password:
        headers["X-Admin-Token"] = admin_password
        headers["x-admin-auth"] = admin_password
        if not token and prefer_bearer:
            headers["Authorization"] = f"Bearer {admin_password}"
    return headers












def _extract_adobe_code_or_link(text):
    if not text:
        return None, None

    text = re.sub(r"(?im)^(source|from|to|received|return-path|message-id|arc-|dkim-|x-|feedback-id|date):.*$", "", str(text))
    code_patterns = [
        r"(?:Your\s+)?verification code is[:\s\r\n]+(?<![A-Za-z0-9])(\d{6})(?![A-Za-z0-9])",
        r"(?:Adobe|verification|verify|identity|code|passcode)[^\d]{0,120}(?<![A-Za-z0-9])(\d{6})(?![A-Za-z0-9])",
        r"(?<![A-Za-z0-9])(\d{6})(?![A-Za-z0-9])",
    ]
    for pattern in code_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1), None

    link_patterns = [
        r"https://[^\s\"'<>]+adobe[^\s\"'<>]+",
        r"https://[^\s\"'<>]+account[^\s\"'<>]+",
    ]
    for pattern in link_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            link = match.group(0).replace("&amp;", "&")
            if re.search(r"adobe-account-access-app-store|/go/", link, re.I):
                continue
            return None, link

    return None, None


def _extract_outlook_adobe_code_or_link(text):
    text = html.unescape(str(text or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    code_patterns = [
        r"(?:Your\s+)?verification code is[:\s\r\n]+(?<![A-Za-z0-9])(\d{6})(?![A-Za-z0-9])",
        r"(?:Adobe|verification|verify|identity|code|passcode)[^\d]{0,120}(?<![A-Za-z0-9])(\d{6})(?![A-Za-z0-9])",
        r"(?<![A-Za-z0-9])(\d{6})(?![A-Za-z0-9])",
    ]
    for pattern in code_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            code = match.group(1)
            if code != "000000" and len(set(code)) > 1:
                return code, None
    return _extract_adobe_code_or_link(text)


def _response_error(response):
    text = response.text[:500]
    try:
        data = response.json()
        if isinstance(data, dict):
            return data.get("error_description") or data.get("error", {}).get("message") or data.get("error") or text
    except Exception:
        pass
    return text or f"HTTP {response.status_code}"


def _exchange_microsoft_refresh_token(refresh_token, client_id):
    import requests

    strategies = [
        (
            "entra-common-delegated",
            "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            {"scope": "offline_access https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/User.Read"},
        ),
        (
            "entra-consumers-delegated",
            "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
            {"scope": "offline_access https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/User.Read"},
        ),
        (
            "entra-common-default",
            "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            {"scope": "https://graph.microsoft.com/.default"},
        ),
        ("entra-common-outlook", "https://login.microsoftonline.com/common/oauth2/v2.0/token", {}),
    ]
    errors = []
    for name, url, extra in strategies:
        payload = {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            **extra,
        }
        for attempt in range(3):
            try:
                res = requests.post(url, data=payload, timeout=25, proxies=_REQUESTS_PROXIES)
                if res.status_code == 200 and res.json().get("access_token"):
                    data = res.json()
                    data["token_strategy"] = name
                    return data
                errors.append(f"{name}: {_response_error(res)}")
                break
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("Microsoft token exchange failed: " + " | ".join(errors[-2:]))


def _normalize_microsoft_message(message, mailbox):
    sender = message.get("From") or message.get("from") or {}
    email_address = sender.get("EmailAddress") or sender.get("emailAddress") or {}
    body = message.get("Body") or message.get("body") or {}
    return {
        "mailbox": mailbox,
        "subject": str(message.get("Subject") or message.get("subject") or ""),
        "sender": str(email_address.get("Address") or email_address.get("address") or ""),
        "preview": str(message.get("BodyPreview") or message.get("bodyPreview") or ""),
        "body": str(body.get("Content") or body.get("content") or ""),
        "received": str(message.get("ReceivedDateTime") or message.get("receivedDateTime") or ""),
    }


def _fetch_microsoft_messages(access_token, transport="graph", mailbox="inbox", top=20):
    import requests

    folder = "junkemail" if mailbox.lower().startswith("junk") else "inbox"
    if transport == "graph":
        url = (
            "https://graph.microsoft.com/v1.0/me/mailFolders/"
            f"{folder}/messages?$top={top}&$select=id,internetMessageId,subject,from,bodyPreview,body,receivedDateTime"
            "&$orderby=receivedDateTime desc"
        )
    else:
        url = (
            "https://outlook.office.com/api/v2.0/me/mailfolders/"
            f"{folder}/messages?$top={top}&$select=Id,Subject,From,BodyPreview,Body,ReceivedDateTime"
            "&$orderby=ReceivedDateTime desc"
        )
    last_error = None
    for attempt in range(3):
        try:
            res = requests.get(url, headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"}, timeout=25, proxies=_REQUESTS_PROXIES)
            break
        except Exception as exc:
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    else:
        raise RuntimeError(f"{transport}/{folder}: {last_error}")
    if res.status_code != 200:
        raise RuntimeError(f"{transport}/{folder}: {_response_error(res)}")
    return [_normalize_microsoft_message(item, folder) for item in (res.json().get("value") or [])]


def _fetch_outlook_adobe_messages(refresh_token, client_id):
    token = _exchange_microsoft_refresh_token(refresh_token, client_id)
    access_token = token["access_token"]
    strategy = token.get("token_strategy") or ""
    transports = ["outlook"] if strategy == "entra-common-outlook" else ["graph", "outlook"]
    errors = []
    for transport in transports:
        messages = []
        try:
            for mailbox in ("inbox", "junkemail"):
                messages.extend(_fetch_microsoft_messages(access_token, transport=transport, mailbox=mailbox, top=20))
            if messages:
                print(f"[Mail] Outlook fetched {len(messages)} messages via {transport}/{strategy}", flush=True)
            return messages
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError("Microsoft mailbox request failed: " + " | ".join(errors[-2:]))


def _maybe_resend_adobe_code(page, start, resend_triggered, source_label="Mail", resend_after_seconds=None):
    if page is None:
        return resend_triggered
    now = time.time()
    if isinstance(resend_triggered, tuple):
        last_resend_at, resend_count = resend_triggered
    elif isinstance(resend_triggered, (int, float)) and not isinstance(resend_triggered, bool):
        last_resend_at, resend_count = float(resend_triggered), 1
    else:
        last_resend_at, resend_count = 0.0, 0
    if resend_count >= 2:
        return resend_triggered
    since_baseline = now - (last_resend_at or start)
    # 阈值可配置，默认 35s：Adobe 邮件通常 5-25s 到达，超过 35s 大概率丢失，立即重发更快
    threshold = resend_after_seconds
    if threshold is None:
        threshold = _config_int("firefly_resend_after_seconds", 35)
    threshold = max(15, int(threshold))
    if since_baseline < threshold:
        return resend_triggered
    print(f"[Mail] no {source_label} Adobe email yet ({int(now - start)}s); clicking Resend Code", flush=True)
    if _click_adobe_resend_code(page):
        return (time.time(), resend_count + 1)
    return resend_triggered


def _received_epoch(received):
    """把邮件 receivedDateTime(ISO/UTC) 解析成 epoch 秒；解析不出返回 None。"""
    s = str(received or "").strip()
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _wait_for_outlook_adobe_email(refresh_token, client_id, timeout=180, page=None, skip_ids=None, resend_triggered=False, blocked_codes=None, fresh_after_ts=None):
    print(f"[Mail] waiting Adobe verification email via Outlook, timeout={timeout}s", flush=True)
    skip_ids = set(skip_ids or [])
    blocked_codes = {str(item).strip() for item in (blocked_codes or []) if str(item).strip()}
    start = time.time()
    last_error = None
    prev_resend = resend_triggered
    while time.time() - start < timeout:
        resend_triggered = _maybe_resend_adobe_code(
            page,
            start,
            resend_triggered,
            source_label="Outlook",
            resend_after_seconds=50 if resend_triggered else None,
        )
        # 刚兜底点了 Resend：Adobe 作废之前所有码，只有 resend 之后新到的码才有效。
        # 把当前收件箱所有邮件加进 skip_ids、新鲜度下限抬到现在(留 8s 时钟余量) → 只认新码。
        if resend_triggered and resend_triggered != prev_resend:
            prev_resend = resend_triggered
            try:
                for _m in _fetch_outlook_adobe_messages(refresh_token, client_id):
                    _mid = str(_m.get("id") or "")
                    if _mid:
                        skip_ids.add(_mid)
            except Exception:
                pass
            fresh_after_ts = time.time() - 8
            print("[Mail] Resend 已点击，仅接收其之后的新验证码（旧码已作废）", flush=True)
            time.sleep(_mail_poll_interval(6))
            continue
        try:
            messages = _fetch_outlook_adobe_messages(refresh_token, client_id)
            for msg in sorted(messages, key=lambda item: item.get("received") or "", reverse=True):
                # 新鲜度过滤：比"本次验证开始前"还旧的邮件一律跳过（防抓到上一次的 stale 验证码）
                if fresh_after_ts is not None:
                    recv = _received_epoch(msg.get("received"))
                    if recv is not None and recv < fresh_after_ts:
                        continue
                if str(msg.get("id") or "") in skip_ids:
                    continue
                content = "\n".join([msg.get("subject", ""), msg.get("sender", ""), msg.get("preview", ""), msg.get("body", "")])
                if not re.search(r"adobe|firefly|account|verify|verification|code", content, re.I):
                    continue
                code, link = _extract_outlook_adobe_code_or_link(content)
                if code and code in blocked_codes:
                    print(f"[Mail] ignoring blocked Outlook verification code: {code}", flush=True)
                    continue
                if code or link:
                    print(f"[Mail] matched Outlook message: {msg.get('subject', '')[:80]}", flush=True)
                    return code, link
        except Exception as exc:
            last_error = exc
            print(f"[Mail] Outlook poll failed: {exc}", flush=True)

        print(f"[Mail] no Outlook Adobe email yet ({int(time.time() - start)}s/{timeout}s)", flush=True)
        time.sleep(_mail_poll_interval(5))

    if last_error:
        print(f"[Mail] last Outlook error: {last_error}", flush=True)
    return None, None


def _snapshot_outlook_message_ids(refresh_token, client_id):
    try:
        return {
            str(msg.get("id") or "")
            for msg in _fetch_outlook_adobe_messages(refresh_token, client_id)
            if str(msg.get("id") or "")
        }
    except Exception:
        return set()


def _parse_cfworker_mail_token(mail_token):
    text = str(mail_token or "")
    if not text.startswith("cfworker:"):
        return None, None
    payload = text[len("cfworker:"):]
    parts = payload.split(":", 1)
    email = (parts[0] if parts else "").strip()
    token = (parts[1] if len(parts) > 1 else "").strip()
    return email, token


def _snapshot_cfworker_message_ids(reg, email, token=""):
    try:
        session = reg._create_duckmail_session()
        return {
            _mail_item_identity(item)
            for item in _fetch_cfworker_emails(session, email, token)
            if _mail_item_identity(item)
        }
    except Exception:
        return set()


def _response_json_or_none(response):
    try:
        return response.json()
    except Exception:
        return None


def _mail_items_from_payload(data):
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("results", "emails", "items", "messages", "data"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _mail_items_from_payload(value)
            if nested:
                return nested
    if any(key in data for key in ("id", "subject", "content", "html_content", "raw", "preview", "verification_code")):
        return [data]
    return []


def _mail_item_identity(item):
    for key in ("id", "message_id", "messageId", "@id", "internetMessageId"):
        value = item.get(key)
        if value is not None:
            return str(value)
    return ""


def _mail_item_text(item):
    if not isinstance(item, dict):
        return ""
    direct_code = str(item.get("verification_code") or "").strip()
    if re.fullmatch(r"\d{6}", direct_code):
        return f"verification code: {direct_code}"
    values = []
    for key in (
        "subject",
        "title",
        "preview",
        "snippet",
        "text",
        "content",
        "html",
        "htmlContent",
        "html_content",
        "body",
        "raw",
        "source",
        "from",
        "sender",
        "to",
        "to_addrs",
    ):
        value = item.get(key)
        if value:
            values.append(str(value))
    text = html.unescape("\n".join(values))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t\r\f\v]+", " ", text)


def _fetch_cfworker_emails(session, email, token=""):
    import config_loader

    base = _normalize_base_url(getattr(config_loader, "CF_WORKER_DOMAIN", "") or "pengfeiapi.xyz")
    admin_password = (getattr(config_loader, "CF_ADMIN_PASSWORD", "") or "").strip()
    headers = _mail_auth_headers(token=token, admin_password=admin_password)
    errors = []
    endpoints = [
        (f"{base}/api/emails", {"mailbox": email, "limit": 20}),
        (f"{base}/api/mails", {"limit": 20, "offset": 0}),
    ]
    for url, params in endpoints:
        try:
            res = session.get(url, params=params, headers=headers, timeout=12, verify=False)
            if res.status_code != 200:
                errors.append(f"{url}: HTTP {res.status_code} {res.text[:120]}")
                continue
            return _mail_items_from_payload(_response_json_or_none(res))
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    if errors:
        print(f"[Mail] CF Worker poll failed: {' | '.join(errors[-2:])}", flush=True)
    return []


def _fetch_cfworker_email_detail(session, item, token=""):
    msg_id = _mail_item_identity(item)
    if not msg_id:
        return {}
    import config_loader

    base = _normalize_base_url(getattr(config_loader, "CF_WORKER_DOMAIN", "") or "pengfeiapi.xyz")
    admin_password = (getattr(config_loader, "CF_ADMIN_PASSWORD", "") or "").strip()
    headers = _mail_auth_headers(token=token, admin_password=admin_password)
    endpoints = [
        (f"{base}/api/email/{msg_id}", None),
        (f"{base}/api/emails/batch", {"ids": msg_id}),
    ]
    for url, params in endpoints:
        try:
            res = session.get(url, params=params, headers=headers, timeout=12, verify=False)
            if res.status_code != 200:
                continue
            data = _response_json_or_none(res)
            items = _mail_items_from_payload(data)
            if items:
                return items[0]
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return {}


def _item_received_epoch(item):
    """从邮件项里尽量取出收件时间 epoch（字段名不固定，多候选）；取不到返回 None。"""
    if not isinstance(item, dict):
        return None
    for k in ("date", "created_at", "createdAt", "receivedAt", "received", "timestamp", "time", "sent_at", "sentAt"):
        v = item.get(k)
        if v in (None, ""):
            continue
        if isinstance(v, (int, float)):
            return float(v) / 1000 if v > 1e12 else float(v)  # 毫秒→秒
        ep = _received_epoch(v)
        if ep is not None:
            return ep
    return None


def _wait_for_cfworker_adobe_email(reg, email, token="", timeout=180, page=None, skip_ids=None, resend_triggered=False, blocked_codes=None, fresh_after_ts=None):
    print(f"[Mail] waiting Adobe verification email via CF Worker for {email}, timeout={timeout}s", flush=True)
    session = reg._create_duckmail_session()
    old_ids = set(skip_ids or [])
    blocked_codes = {str(item).strip() for item in (blocked_codes or []) if str(item).strip()}
    if not old_ids:
        old_ids = {_mail_item_identity(item) for item in _fetch_cfworker_emails(session, email, token) if _mail_item_identity(item)}
    start = time.time()
    prev_resend = resend_triggered
    while time.time() - start < timeout:
        resend_triggered = _maybe_resend_adobe_code(
            page,
            start,
            resend_triggered,
            source_label="CF Worker",
            resend_after_seconds=50 if resend_triggered else None,
        )
        # 刚兜底点了 Resend：把当前所有邮件加进 old_ids、新鲜度下限抬到现在 → 只认 resend 之后的新码（旧码已作废）。
        if resend_triggered and resend_triggered != prev_resend:
            prev_resend = resend_triggered
            try:
                for _it in _fetch_cfworker_emails(session, email, token):
                    _id = _mail_item_identity(_it)
                    if _id:
                        old_ids.add(_id)
            except Exception:
                pass
            fresh_after_ts = time.time() - 8
            print("[Mail] Resend 已点击，仅接收其之后的新验证码（旧码已作废）", flush=True)
            time.sleep(_mail_poll_interval(4))
            continue
        messages = _fetch_cfworker_emails(session, email, token)
        if messages:
            print(f"[Mail] CF Worker poll returned {len(messages)} messages", flush=True)
        for item in messages:
            identity = _mail_item_identity(item)
            if identity and identity in old_ids:
                continue
            # 新鲜度过滤：比本次验证开始前还旧的邮件跳过（防自定义域名也抓到 stale 验证码）；取不到时间就不过滤
            if fresh_after_ts is not None:
                recv = _item_received_epoch(item)
                if recv is not None and recv < fresh_after_ts:
                    continue
            text = _mail_item_text(item)
            if not re.search(r"adobe|firefly|account|verify|verification|code", text, re.I):
                continue
            code, link = _extract_adobe_code_or_link(text)
            if code and code in blocked_codes:
                print(f"[Mail] ignoring blocked CF Worker verification code: {code}", flush=True)
                continue
            if code or link:
                print(f"[Mail] matched CF Worker message: {str(item.get('subject') or '')[:80]}", flush=True)
                return code, link
            detail = _fetch_cfworker_email_detail(session, item, token)
            detail_text = _mail_item_text(detail)
            code, link = _extract_adobe_code_or_link(detail_text)
            if code and code in blocked_codes:
                print(f"[Mail] ignoring blocked CF Worker verification code from detail: {code}", flush=True)
                continue
            if code or link:
                print(f"[Mail] matched CF Worker message detail: {str(item.get('subject') or detail.get('subject') or '')[:80]}", flush=True)
                return code, link

        print(f"[Mail] no CF Worker Adobe email yet ({int(time.time() - start)}s/{timeout}s)", flush=True)
        time.sleep(_mail_poll_interval(3))
    return None, None


def _wait_for_adobe_email(reg, mail_token, timeout=180, outlook_refresh_token="", outlook_client_id="", page=None, skip_ids=None, resend_triggered=False, blocked_codes=None, fresh_after_ts=None):
    if outlook_refresh_token:
        return _wait_for_outlook_adobe_email(outlook_refresh_token, outlook_client_id, timeout=timeout, page=page, skip_ids=skip_ids, resend_triggered=resend_triggered, blocked_codes=blocked_codes, fresh_after_ts=fresh_after_ts)
    cf_email, cf_token = _parse_cfworker_mail_token(mail_token)
    if cf_email:
        return _wait_for_cfworker_adobe_email(reg, cf_email, cf_token, timeout=timeout, page=page, skip_ids=skip_ids, resend_triggered=resend_triggered, blocked_codes=blocked_codes, fresh_after_ts=fresh_after_ts)

    print(f"[Mail] waiting Adobe verification email, timeout={timeout}s")
    blocked_codes = {str(item).strip() for item in (blocked_codes or []) if str(item).strip()}
    start = time.time()
    while time.time() - start < timeout:
        resend_triggered = _maybe_resend_adobe_code(
            page,
            start,
            resend_triggered,
            resend_after_seconds=50 if resend_triggered else None,
        )
        messages = reg._fetch_emails_duckmail(mail_token) or []
        for msg in messages:
            subject = str(msg.get("subject") or msg.get("title") or "")
            sender = str(msg.get("from") or msg.get("sender") or "")
            inline = " ".join(str(msg.get(k) or "") for k in ("text", "html", "raw", "source", "content"))
            looks_adobe = re.search(r"adobe|firefly|account|verify|code", subject + " " + sender + " " + inline, re.I)
            if not looks_adobe:
                continue

            code, link = _extract_adobe_code_or_link(inline)
            if code and code in blocked_codes:
                print(f"[Mail] ignoring blocked Adobe verification code: {code}", flush=True)
                continue
            if code or link:
                return code, link

            msg_id = msg.get("id") or msg.get("@id") or msg.get("message_id")
            if msg_id:
                detail = reg._fetch_email_detail_duckmail(mail_token, str(msg_id)) or {}
                content = " ".join(str(detail.get(k) or "") for k in ("text", "html", "source", "content", "html_content"))
                code, link = _extract_adobe_code_or_link(content)
                if code and code in blocked_codes:
                    print(f"[Mail] ignoring blocked Adobe verification code from detail: {code}", flush=True)
                    continue
                if code or link:
                    return code, link

        print(f"[Mail] no Adobe email yet ({int(time.time() - start)}s/{timeout}s)")
        time.sleep(_mail_poll_interval(3))

    return None, None


def _click_by_text(page, pattern, timeout=10000):
    candidates = [
        lambda: page.get_by_role("button", name=pattern).first,
        lambda: page.get_by_role("link", name=pattern).first,
        lambda: page.get_by_text(pattern).first,
    ]
    # 支持重试
    max_retries = 3
    retry_interval = 2  # 秒

    for make_locator in candidates:
        for attempt in range(max_retries):
            try:
                loc = make_locator()
                # 先检查是否可见
                if loc.is_visible(timeout=2000):
                    print(f"[Click] 找到元素，尝试点击 (尝试 {attempt + 1}/{max_retries})...")
                    loc.click(timeout=timeout)
                    print(f"[Click] ✓ 点击成功")
                    return True
                else:
                    if attempt < max_retries - 1:
                        print(f"[Click] 元素不可见，{retry_interval}秒后重试...")
                        time.sleep(retry_interval)
            except Exception as e:
                if attempt < max_retries - 1:
                    print(f"[Click] 点击失败: {e}，{retry_interval}秒后重试...")
                    time.sleep(retry_interval)
                else:
                    continue  # 尝试下一个候选

    print(f"[Click] ✗ 未能点击元素")
    return False


























def _wait_for_adobe_login_state(page, timeout=30000):
    deadline = time.time() + max(timeout, 0) / 1000
    last_state = "unknown"
    while time.time() < deadline:
        try:
            body_text = page.locator("body").inner_text(timeout=3000) or ""
        except Exception:
            body_text = ""
        lower = body_text.lower()
        if _looks_logged_in_adobe_account(page) or _looks_verified_or_logged_in_adobe(page):
            return "logged_in"
        if _page_requires_adobe_email_verification(page):
            return "verification"
        if "#/signup" in page.url and "create account" in lower:
            return "signup"
        if "#/error" in page.url or "something went wrong" in lower:
            return "error"
        if "#/password-change/auth" in page.url or "type current password" in lower:
            return "existing_account"
        if re.search(r"couldn.t find|could not find|account not found|no account|create an account", lower, re.I):
            last_state = "not_found"
        elif _visible_password_input_present(page):
            last_state = "password"
        elif _looks_adobe_signin_landing(page):
            last_state = "signin"
        try:
            page.wait_for_timeout(1000)
        except Exception:
            time.sleep(1)
    return last_state






def _fill_and_verify(loc, value, timeout, tries=4):
    """填值 + 回读校验:Adobe 登录页刚加载/hydration 未完成时,太快填进的值会被页面清空
    (报 'Please enter an email address')。填完 sleep 一下回读 input_value,没进去就等一下重填,
    直到值真进去或 tries 用尽。根治"输入太快界面没加载好"。"""
    import time as _t
    want = (value or "").strip()
    for _ in range(tries):
        try:
            loc.scroll_into_view_if_needed(timeout=1000)
            loc.fill(value, timeout=timeout)
            _t.sleep(0.35)
            if (loc.input_value() or "").strip() == want:
                return True
        except Exception:
            pass
        _t.sleep(0.6)   # 页面还在加载/re-render → 等一下再重填
    return False


def _fill_first(page, selectors, value, timeout=8000):
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            loc.wait_for(state="visible", timeout=timeout)   # ★等【可见】(不只attached),防页面没加载好就填空值
            if _fill_and_verify(loc, value, timeout):
                return True
        except Exception:
            continue
    return False














def _fill_by_label(page, label, value, timeout=5000):
    try:
        loc = page.get_by_label(re.compile(label, re.I)).first
        loc.wait_for(state="visible", timeout=timeout)
        return _fill_and_verify(loc, value, timeout)   # ★回读校验+重填,防填太快值被页面清空
    except Exception:
        return False


def _visible_password_input_present(page):
    try:
        return bool(page.evaluate(
            """() => Array.from(document.querySelectorAll('input[type="password"], input[name*="password" i], input[id*="password" i]')).some((node) => {
                const box = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return box.width > 0 && box.height > 0
                  && style.display !== 'none'
                  && style.visibility !== 'hidden'
                  && !node.disabled;
            })"""
        ))
    except Exception:
        return False


def _fill_adobe_signin_email(page, email, timeout=10000):
    return (
        _fill_by_label(page, r"Email address|Email", email, timeout=timeout)
        or _fill_first(page, [
            "#EmailPage-EmailField",
            "input[type='email']",
            "input[name='username']",
            "input[name='email']",
            "input[id*='email' i]",
            "input[autocomplete='username']",
        ], email, timeout=timeout)
    )


def _fill_adobe_signin_password(page, password, timeout=10000):
    return (
        _fill_by_label(page, r"Password", password, timeout=timeout)
        or _fill_first(page, [
            "#PasswordPage-PasswordField",
            "input[type='password']",
            "input[name='password']",
            "input[id*='password' i]",
            "input[autocomplete='current-password']",
        ], password, timeout=timeout)
    )


















































































def _looks_logged_in_adobe_account(page):
    try:
        url = page.url.lower()
        body_text = page.locator("body").inner_text(timeout=1000) or ""
    except Exception:
        return False
    lower = body_text.lower()
    blocking_markers = (
        "create account",
        "sign in or create an account",
        "sign up with email",
        "already have an account",
        "email address",
        "password strength",
        "verify your identity",
        "verify your email",
        "enter code",
        "verification code",
        "resend code",
        "type current password",
        "that's an invalid password",
        "创建账户",
        "创建帐户",
        "验证您的身份",
        "验证你的身份",
        "验证码",
    )
    account_home_detected = (
        "account.adobe.com" in url
        and (
            "welcome to your account" in lower
            or ("overview" in lower and "edit profile" in lower)
            or "adobe free membership" in lower
        )
    )
    account_home_blockers = tuple(marker for marker in blocking_markers if marker != "email address")
    if account_home_detected and not any(marker in lower for marker in account_home_blockers):
        return True
    if (
        "#/signup" in url
        or "#/challenge" in url
        or "#/email-verification" in url
        or "#/password-change" in url
        or "#/error" in url
        or any(marker in lower for marker in blocking_markers)
    ):
        return False
    return account_home_detected


def _looks_adobe_signin_landing(page):
    try:
        url = page.url.lower()
        body_text = page.locator("body").inner_text(timeout=1000) or ""
    except Exception:
        return False
    lower = body_text.lower()
    if "auth.services.adobe.com" not in url and "account.adobe.com" not in url:
        return False
    sign_in_markers = (
        "sign in or create an account",
        "new user? create an account",
        "email address",
        "continue with google",
        "get help signing in",
    )
    verification_input_markers = (
        "enter code",
        "verification code",
        "resend code",
        "check your email",
        "verify your email",
    )
    return (
        ("sign in" in lower or "create an account" in lower)
        and any(marker in lower for marker in sign_in_markers)
        and not any(marker in lower for marker in verification_input_markers)
    )


def _page_requires_adobe_email_verification(page):
    try:
        url = page.url.lower()
        body_text = page.locator("body").inner_text(timeout=1000) or ""
    except Exception:
        return False
    lower = body_text.lower()
    if _looks_logged_in_adobe_account(page):
        return False
    if _looks_adobe_signin_landing(page):
        return False
    if _looks_verified_or_logged_in_adobe(page):
        return False
    verification_markers = (
        "verify your identity",
        "verify your email",
        "enter code",
        "verification code",
        "check your email",
        "email verification",
        "resend code",
        "验证您的身份",
        "验证你的身份",
        "请输入我们刚才发送",
        "发送到以下邮箱的代码",
        "重新发送代码",
        "验证码",
    )
    return (
        any(marker in lower for marker in verification_markers)
        and ("account.adobe.com" in url or "auth.services.adobe.com" in url)
    )


def _looks_verified_or_logged_in_adobe(page):
    try:
        url = page.url.lower()
        body_text = page.locator("body").inner_text(timeout=1000) or ""
    except Exception:
        return False
    lower = body_text.lower()
    if _looks_logged_in_adobe_account(page):
        return True
    if "firefly.adobe.com" in url and not re.search(r"\b(sign in|log in|login)\b|登录|登入", lower):
        return True
    if (
        ("account.adobe.com" in url or "auth.services.adobe.com" in url)
        and "#/signup" not in url
        and "#/challenge" not in url
        and "#/email-verification" not in url
    ):
        markers = (
            "account",
            "profile",
            "security",
            "privacy",
            "plans",
            "welcome",
            "personal information",
            "manage account",
        )
        negatives = (
            "create account",
            "sign up",
            "verify your email",
            "verify your identity",
            "enter code",
            "verification code",
            "type current password",
            "sign in or create an account",
            "sign up with email",
            "already have an account",
            "email address",
            "password strength",
            "that's an invalid password",
            "创建账户",
            "创建帐户",
            "验证您的身份",
            "验证你的身份",
            "请输入我们刚才发送",
            "发送到以下邮箱的代码",
            "重新发送代码",
            "验证码",
        )
        return any(marker in lower for marker in markers) and not any(neg in lower for neg in negatives)
    return False


def _ims_access_token(page):
    """读当前页 IMS access_token（已登录即有；FF 秒导出同款就绪信号）。"""
    try:
        tok = page.evaluate(
            "()=>{try{var t=window.adobeIMS&&window.adobeIMS.getAccessToken&&window.adobeIMS.getAccessToken();return t&&t.token?t.token:'';}catch(e){return '';}}"
        )
        return tok if (tok and str(tok).startswith("ey")) else ""
    except Exception:
        return ""


def _firefly_ready_for_cookie_export(page):
    try:
        url = page.url.lower()
        if "firefly.adobe.com" not in url:
            return False
    except Exception:
        return False
    # 正向信号优先：有 IMS access_token = 已登录可导，避开 What's new 弹窗 / 「Sign in」标题误判
    if _ims_access_token(page):
        return True
    try:
        body_text = page.locator("body").inner_text(timeout=3000) or ""
    except Exception:
        return False
    lower = body_text.lower()
    blocked_markers = (
        "sign in",
        "log in",
        "login",
        "verify your identity",
        "verify your email",
        "verification code",
        "enter code",
        "check your email",
        "登录",
        "登入",
        "验证码",
    )
    return not any(marker in lower for marker in blocked_markers)


def _cookie_header_stats(cookie_header):
    names = []
    for part in str(cookie_header or "").split(";"):
        name = part.split("=", 1)[0].strip()
        if name:
            names.append(name)
    lowered = {name.lower() for name in names}
    required = {"ims_sid", "aux_sid", "relay"}
    return {
        "length": len(cookie_header or ""),
        "count": len(names),
        "missing": sorted(required - lowered),
    }


def _wait_for_firefly_cookie_export_ready(page, context, timeout=45000):
    deadline = time.time() + max(timeout, 0) / 1000
    stable_seen = 0
    last_signature = None
    last_reason = "not checked"
    min_length = _config_int("firefly_cookie_export_min_length", 0, minimum=0, maximum=10000)
    min_count = _config_int("firefly_cookie_export_min_count", 0, minimum=0, maximum=200)
    # 必需 cookie 齐了要连续几次读一致才导。默认 1 = 立刻导（不等稳定，提速）。想保险设 2。
    min_stable = _config_int("firefly_cookie_export_stable", 1, minimum=1, maximum=5)

    while time.time() < deadline:
        session_page = _find_firefly_session_page(context, preferred_page=page)
        if session_page is not None:
            page = session_page

        verification_page = _find_adobe_email_verification_page(context, preferred_page=page)
        if verification_page is not None:
            last_reason = "Adobe verification page is still visible"
            stable_seen = 0
        elif not _firefly_ready_for_cookie_export(page):
            last_reason = f"Firefly page is not ready: {getattr(page, 'url', '')}"
            stable_seen = 0
        else:
            cookie_header, skipped = _adobe2api_cookie_header_from_context(context)
            stats = _cookie_header_stats(cookie_header)
            if stats["missing"]:
                last_reason = f"missing required cookies: {', '.join(stats['missing'])}"
                stable_seen = 0
            elif (min_length and stats["length"] < min_length) or (min_count and stats["count"] < min_count):
                length_target = str(min_length) if min_length else "off"
                count_target = str(min_count) if min_count else "off"
                last_reason = (
                    f"cookie header not complete enough yet: "
                    f"length={stats['length']}/{length_target}, count={stats['count']}/{count_target}"
                )
                stable_seen = 0
            else:
                signature = (stats["length"], stats["count"], hash(cookie_header))
                if signature == last_signature:
                    stable_seen += 1
                else:
                    stable_seen = 1
                    last_signature = signature
                last_reason = (
                    f"cookie ready candidate: length={stats['length']}, "
                    f"count={stats['count']}, stable={stable_seen}/{min_stable}"
                )
                if stable_seen >= min_stable:
                    if skipped:
                        print(
                            f"[Cookie] verification cookies present but excluded: {', '.join(sorted(set(skipped)))}",
                            flush=True,
                        )
                    print(f"[Cookie] Firefly export readiness confirmed: {last_reason}", flush=True)
                    return page

        try:
            page.wait_for_timeout(500)
        except Exception:
            time.sleep(0.5)

    print(f"[Cookie] Firefly export readiness timed out: {last_reason}", flush=True)
    return None


def _open_firefly_for_cookie_export(page, reason=""):
    target = "https://firefly.adobe.com/"
    suffix = f" ({reason})" if reason else ""
    try:
        page.bring_to_front()
    except Exception:
        pass
    try:
        print(f"[Browser] opening Firefly{suffix}", flush=True)
        page.goto(target, wait_until="commit", timeout=20000)
    except Exception as exc:
        print(f"[Browser] Firefly goto failed{suffix}: {exc}; retrying with location.href", flush=True)
        try:
            page.evaluate("url => { window.location.href = url; }", target)
            page.wait_for_timeout(1500)
        except Exception as retry_exc:
            print(f"[Browser] Firefly location.href retry failed{suffix}: {retry_exc}", flush=True)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=4000)
    except Exception:
        pass
    try:
        if "firefly.adobe.com" not in page.url.lower():
            page.goto(target, wait_until="commit", timeout=15000)
    except Exception as exc:
        print(f"[Browser] Firefly second goto failed{suffix}: {exc}", flush=True)
    try:
        page.wait_for_timeout(500)
    except Exception:
        time.sleep(0.5)
    return "firefly.adobe.com" in (getattr(page, "url", "") or "").lower()


def _dismiss_firefly_popups(page):
    if "firefly.adobe.com" not in page.url.lower():
        return
    for _ in range(3):
        try:
            result = page.evaluate(
                """() => {
                    const visible = (node) => {
                      const box = node.getBoundingClientRect();
                      const style = getComputedStyle(node);
                      return box.width > 0 && box.height > 0
                        && box.right > 0 && box.bottom > 0
                        && box.left < innerWidth && box.top < innerHeight
                        && style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && Number(style.opacity || 1) > 0.05;
                    };
                    const textOf = (node) => (node.innerText || node.textContent || '').trim();
                    const candidates = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], div, section'));
                    const dialog = candidates
                      .filter((node) => visible(node) && /what\\s*'?s\\s+new/i.test(textOf(node)))
                      .map((node) => ({ node, box: node.getBoundingClientRect() }))
                      .filter((item) => item.box.width > 300 && item.box.height > 250)
                      .sort((a, b) => (b.box.width * b.box.height) - (a.box.width * a.box.height))[0];
                    if (!dialog) return { clicked: false, reason: 'no-whats-new-dialog' };

                    const box = dialog.box;
                    const controls = Array.from(dialog.node.querySelectorAll('button, [role="button"], [aria-label], svg'))
                      .filter(visible)
                      .map((node) => ({ node, box: node.getBoundingClientRect(), text: textOf(node), label: node.getAttribute('aria-label') || '' }))
                      .filter((item) => {
                        const nearTopRight = item.box.left > box.right - 100 && item.box.top < box.top + 90;
                        const looksClose = /close|dismiss|\\u00d7|^x$/i.test(`${item.text} ${item.label}`);
                        return nearTopRight || looksClose;
                      })
                      .sort((a, b) => {
                        const da = Math.abs(a.box.right - box.right) + Math.abs(a.box.top - box.top);
                        const db = Math.abs(b.box.right - box.right) + Math.abs(b.box.top - box.top);
                        return da - db;
                      });
                    if (controls[0]) {
                      controls[0].node.click();
                      return { clicked: true, method: 'button' };
                    }

                    const target = document.elementFromPoint(Math.max(0, box.right - 28), Math.max(0, box.top + 26));
                    if (target) {
                      target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                      return { clicked: true, method: 'elementFromPoint' };
                    }
                    return { clicked: false, reason: 'no-target' };
                }"""
            )
            if isinstance(result, dict) and result.get("clicked"):
                print(f"[Firefly] dismissed What's new popup via {result.get('method')}", flush=True)
                page.wait_for_timeout(500)
                return
        except Exception:
            pass
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception:
            pass
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
    except Exception:
        pass
    selectors = [
        "button[aria-label='Close']",
        "button[aria-label*='close' i]",
        "[role='dialog'] button[aria-label*='close' i]",
        "[aria-modal='true'] button[aria-label*='close' i]",
        "[role='dialog'] button:has-text('×')",
        "[aria-modal='true'] button:has-text('×')",
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() > 0 and loc.is_visible(timeout=500):
                loc.click(timeout=800, force=True)
                page.wait_for_timeout(250)
                return
        except Exception:
            pass
    try:
        page.evaluate(
            """() => {
                const visible = (node) => {
                  const box = node.getBoundingClientRect();
                  const style = getComputedStyle(node);
                  return box.width > 0 && box.height > 0
                    && style.display !== 'none'
                    && style.visibility !== 'hidden';
                };
                const buttons = Array.from(document.querySelectorAll('button,[role="button"]'));
                const close = buttons.find((node) => {
                  const text = (node.innerText || node.textContent || node.getAttribute('aria-label') || '').trim();
                  return visible(node) && (/^×$|^x$|close/i.test(text) || /close/i.test(node.getAttribute('aria-label') || ''));
                });
                if (close) close.click();
            }"""
        )
    except Exception:
        pass


def _wait_for_adobe_account_home(page, timeout=15000):
    print(f"[Browser] waiting for Adobe account home after verification, timeout={timeout // 1000}s", flush=True)
    deadline = time.time() + max(timeout, 0) / 1000
    navigated = False
    while time.time() < deadline:
        if _looks_verified_or_logged_in_adobe(page):
            print(f"[Browser] Adobe verified/account page detected: {page.url}", flush=True)
            return True
        try:
            if not navigated and (page.url.lower().endswith("#/") or "#/" not in page.url.lower()):
                navigated = True
                page.goto("https://account.adobe.com/#/", wait_until="domcontentloaded", timeout=30000)
            else:
                page.wait_for_timeout(1000)
        except Exception:
            time.sleep(1)
    return _looks_verified_or_logged_in_adobe(page)




def _find_adobe_email_verification_page(context, preferred_page=None):
    pages = []
    if preferred_page is not None:
        pages.append(preferred_page)
    try:
        for item in context.pages:
            if item not in pages:
                pages.append(item)
    except Exception:
        pass
    for item in pages:
        try:
            if item.is_closed():
                continue
        except Exception:
            continue
        if _page_requires_adobe_email_verification(item):
            return item
    return None


def _find_firefly_session_page(context, preferred_page=None):
    pages = []
    if preferred_page is not None:
        pages.append(preferred_page)
    try:
        for item in context.pages:
            if item not in pages:
                pages.append(item)
    except Exception:
        pass
    for item in pages:
        try:
            if item.is_closed():
                continue
            url = item.url.lower()
            if "firefly.adobe.com" in url or "access_token=" in url:
                return item
        except Exception:
            continue
    return None


def _open_adobe_email_verification_challenge(page):
    try:
        page.goto(
            "https://auth.services.adobe.com/en_US/deeplink.html#/challenge/email-verification/code",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        try:
            page.wait_for_timeout(1500)
        except Exception:
            time.sleep(1.5)
        return _page_requires_adobe_email_verification(page)
    except Exception as exc:
        print(f"[Mail] failed to open Adobe email verification challenge: {exc}", flush=True)
        return False


def _click_adobe_resend_code(page):
    selectors = [
        "button:has-text('Resend Code')",
        "button:has-text('Resend code')",
        "[role='button']:has-text('Resend Code')",
        "[role='button']:has-text('Resend code')",
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() > 0 and loc.is_visible(timeout=1000):
                loc.click(timeout=3000, force=True)
                print("[Mail] clicked Adobe Resend Code button", flush=True)
                try:
                    page.wait_for_timeout(1200)
                except Exception:
                    time.sleep(1.2)
                return True
        except Exception:
            pass

    try:
        clicked = page.evaluate(
            """() => {
                const visible = (node) => {
                  const box = node.getBoundingClientRect();
                  const style = getComputedStyle(node);
                  return box.width > 0 && box.height > 0
                    && style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && !node.disabled;
                };
                const controls = Array.from(document.querySelectorAll('button,[role="button"],a'))
                  .filter(visible);
                const target = controls.find((node) =>
                  /resend\\s+code/i.test((node.innerText || node.textContent || '').trim())
                );
                if (!target) return false;
                target.click();
                return true;
            }"""
        )
        if clicked:
            print("[Mail] clicked Adobe Resend Code button", flush=True)
            try:
                page.wait_for_timeout(1200)
            except Exception:
                time.sleep(1.2)
            return True
    except Exception:
        pass
    print("[Mail] Adobe Resend Code button not found; polling mailbox anyway", flush=True)
    return False


def _fill_adobe_email_code(page, code):
    code = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        return False

    # Prefer real keyboard input. Adobe's six-box code UI may ignore direct JS value writes.
    try:
        focused = page.evaluate(
            """() => {
                const visible = (node) => {
                  const box = node.getBoundingClientRect();
                  const style = getComputedStyle(node);
                  return box.width > 0 && box.height > 0
                    && style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && !node.disabled;
                };
                const inputs = Array.from(document.querySelectorAll('input'))
                  .filter((node) => visible(node)
                    && !['hidden', 'checkbox', 'radio', 'submit', 'button', 'password', 'email'].includes(String(node.type || '').toLowerCase()));
                const oneChar = inputs.filter((node) => {
                  const maxLength = Number(node.maxLength || node.getAttribute('maxlength') || 0);
                  const box = node.getBoundingClientRect();
                  return maxLength === 1 || (box.width <= 90 && box.height >= 35);
                });
                const target = oneChar[0] || inputs[0];
                if (!target) return false;
                target.focus();
                target.click();
                return true;
            }"""
        )
        if focused:
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            page.keyboard.type(code, delay=80)
            try:
                page.wait_for_timeout(1200)
            except Exception:
                pass
            if not _page_requires_adobe_email_verification(page):
                return True
    except Exception:
        pass

    # Adobe sometimes renders six one-character boxes instead of one code field.
    try:
        filled = page.evaluate(
            """(code) => {
                const visible = (node) => {
                  const box = node.getBoundingClientRect();
                  const style = getComputedStyle(node);
                  return box.width > 0 && box.height > 0
                    && style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && !node.disabled;
                };
                const setValue = (node, value) => {
                  node.focus();
                  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                  if (setter) {
                    setter.call(node, value);
                  } else {
                    node.value = value;
                  }
                  node.dispatchEvent(new Event('input', { bubbles: true }));
                  node.dispatchEvent(new Event('change', { bubbles: true }));
                  node.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: value }));
                };
                const allInputs = Array.from(document.querySelectorAll('input'))
                  .filter((node) => visible(node)
                    && !['hidden', 'checkbox', 'radio', 'submit', 'button', 'password', 'email'].includes(String(node.type || '').toLowerCase()));
                const codeInputs = allInputs.filter((node) => {
                  const attrs = [
                    node.name,
                    node.id,
                    node.autocomplete,
                    node.placeholder,
                    node.getAttribute('aria-label'),
                  ].join(' ');
                  return /code|otp|verification|passcode|验证码|代码/i.test(attrs);
                });
                if (codeInputs.length === 1) {
                  setValue(codeInputs[0], code);
                  return 'single';
                }
                const oneCharInputs = allInputs.filter((node) => {
                  const maxLength = Number(node.maxLength || node.getAttribute('maxlength') || 0);
                  const box = node.getBoundingClientRect();
                  return maxLength === 1 || (box.width <= 90 && box.height >= 35);
                });
                const targets = oneCharInputs.length >= code.length ? oneCharInputs : allInputs;
                if (targets.length >= code.length) {
                  for (let i = 0; i < code.length; i += 1) {
                    setValue(targets[i], code[i]);
                  }
                  return 'split';
                }
                return '';
            }""",
            code,
        )
        if filled:
            try:
                page.wait_for_timeout(800)
            except Exception:
                pass
            return True
    except Exception:
        pass

    selectors = [
        "input[name*='code' i]",
        "input[id*='code' i]",
        "input[autocomplete='one-time-code']",
        "input[type='tel']",
        "input[type='text']",
    ]
    if _fill_first(page, selectors, code, timeout=10000):
        return True

    try:
        loc = page.locator("input").first
        loc.click(timeout=3000, force=True)
        page.keyboard.press("Control+A")
        page.keyboard.type(code, delay=40)
        return True
    except Exception:
        return False


def _wait_until_page_not_verifying(page, timeout=45000):
    deadline = time.time() + max(timeout, 0) / 1000
    while time.time() < deadline:
        try:
            if page.is_closed():
                return True
        except Exception:
            return True
        if not _page_requires_adobe_email_verification(page):
            return True
        try:
            page.wait_for_timeout(1000)
        except Exception:
            time.sleep(1)
    return not _page_requires_adobe_email_verification(page)


def _confirm_adobe_email_verification_completed(page, context=None, timeout=45000):
    deadline = time.time() + max(timeout, 0) / 1000
    while time.time() < deadline:
        pages = [page]
        if context is not None:
            try:
                for item in context.pages:
                    if item not in pages:
                        pages.append(item)
            except Exception:
                pass

        for item in pages:
            try:
                if item.is_closed():
                    continue
                if _page_requires_adobe_email_verification(item):
                    continue
                if _looks_logged_in_adobe_account(item) or _looks_verified_or_logged_in_adobe(item):
                    return item
            except Exception:
                continue

        try:
            page.wait_for_timeout(1000)
        except Exception:
            time.sleep(1)

    try:
        if _wait_for_adobe_account_home(page, timeout=15000):
            return page
    except Exception:
        pass
    return None


def _wait_for_manual_email_verification_completion(page, context, verification_state, timeout=600000):
    print(
        f"[Manual] email verification is required before cookie export; "
        f"please complete the email code in the browser (timeout={timeout // 1000}s)",
        flush=True,
    )
    deadline = time.time() + max(timeout, 0) / 1000
    saw_verification_page = bool((verification_state or {}).get("saw_page"))
    last_notice = 0
    last_active = page
    while time.time() < deadline:
        pages = []
        try:
            pages = [item for item in context.pages if not item.is_closed()]
        except Exception:
            pages = [page]

        for item in pages:
            if _page_requires_adobe_email_verification(item):
                saw_verification_page = True
                if verification_state is not None:
                    verification_state["saw_page"] = True
                last_active = item

        if saw_verification_page:
            for item in pages:
                if _page_requires_adobe_email_verification(item):
                    continue
                if _looks_logged_in_adobe_account(item) or _firefly_ready_for_cookie_export(item):
                    if verification_state is not None:
                        verification_state["done"] = True
                    print("[Manual] email verification completed in browser; continuing", flush=True)
                    return item

        now = time.time()
        if now - last_notice >= 30:
            remain = int(deadline - now)
            print(f"[Manual] waiting for manual email verification, remaining={remain}s", flush=True)
            last_notice = now
        try:
            last_active.wait_for_timeout(1000)
        except Exception:
            time.sleep(1)
    return None


def _handle_firefly_email_verification(
    page,
    context,
    reg,
    mail_token,
    outlook_refresh_token="",
    outlook_client_id="",
    timeout=180,
    detection_timeout=10,
    verification_state=None,
    require_verification=False,
):
    detection_deadline = time.time() + max(detection_timeout, 0)
    verification_page = None
    while time.time() < detection_deadline:
        verification_page = _find_adobe_email_verification_page(context, preferred_page=page)
        if verification_page is not None:
            break
        try:
            page.wait_for_timeout(1000)
        except Exception:
            time.sleep(1)

    if verification_page is None and require_verification:
        print("[Firefly] identity verification did not appear; forcing Adobe email verification challenge", flush=True)
        if _open_adobe_email_verification_challenge(page):
            verification_page = page
        else:
            verification_page = _find_adobe_email_verification_page(context, preferred_page=page)

    if verification_page is None:
        if require_verification:
            print("[Firefly] email verification page could not be opened; cookie export is blocked", flush=True)
        else:
            print(f"[Firefly] no identity verification page within {detection_timeout}s; continuing to cookie export", flush=True)
        return not require_verification

    print(f"[Firefly] Adobe identity verification detected: {verification_page.url}", flush=True)
    if verification_state is not None:
        verification_state["saw_page"] = True
    skip_ids = set()
    if outlook_refresh_token:
        skip_ids = _snapshot_outlook_message_ids(outlook_refresh_token, outlook_client_id)
    else:
        cf_email, cf_token = _parse_cfworker_mail_token(mail_token)
        if cf_email:
            skip_ids = _snapshot_cfworker_message_ids(reg, cf_email, cf_token)
    if skip_ids:
        print(f"[Mail] ignoring {len(skip_ids)} existing verification message(s) before resend", flush=True)
    resend_clicked = _click_adobe_resend_code(verification_page)
    resend_triggered_at = (time.time(), 1) if resend_clicked else False
    if not resend_clicked and skip_ids:
        print("[Mail] Resend Code was not clicked; accepting existing verification messages", flush=True)
        skip_ids = set()
    code, link = _wait_for_adobe_email(
        reg,
        mail_token,
        timeout=timeout,
        outlook_refresh_token=outlook_refresh_token,
        outlook_client_id=outlook_client_id,
        page=verification_page,
        skip_ids=skip_ids,
        resend_triggered=resend_triggered_at,
    )
    if link == ADOBE_ALREADY_VERIFIED:
        print("[Firefly] Adobe page is already verified while polling email", flush=True)
        confirmed_page = _confirm_adobe_email_verification_completed(page, context, timeout=30000)
        if confirmed_page is not None:
            if verification_state is not None:
                verification_state["done"] = True
            _open_firefly_for_cookie_export(confirmed_page, reason="already verified")
            return True
        print("[Firefly] already-verified state was not confirmed; cookie export remains blocked", flush=True)
        return False
    if link:
        print(f"[Firefly] verification link found: {link}", flush=True)
        verification_page.goto(link, wait_until="commit", timeout=15000)
        try:
            verification_page.wait_for_timeout(1200)
        except Exception:
            time.sleep(1.2)
        if not _wait_until_page_not_verifying(verification_page, timeout=25000):
            print("[Firefly] verification link did not clear Adobe verification page", flush=True)
            return False
        confirmed_page = _confirm_adobe_email_verification_completed(verification_page, context, timeout=30000)
        if confirmed_page is None:
            print("[Firefly] verification link cleared the code page, but logged-in state was not confirmed", flush=True)
            return False
        if verification_state is not None:
            verification_state["done"] = True
        page = confirmed_page
    elif code:
        print(f"[Firefly] identity verification code found: {code}", flush=True)
        if not _fill_adobe_email_code(verification_page, code):
            return False
        try:
            verification_page.keyboard.press("Enter")
            print("[Firefly] pressed Enter after Adobe email code input", flush=True)
        except Exception:
            pass
        try:
            verification_page.wait_for_timeout(2500)
        except Exception:
            time.sleep(2.5)
        session_page = _find_firefly_session_page(context, preferred_page=verification_page)
        if session_page is not None:
            print(f"[Firefly] Firefly session page detected after code submit: {session_page.url}", flush=True)
            ready_page = _wait_for_firefly_cookie_export_ready(session_page, context, timeout=45000)
            if ready_page is None:
                print("[Firefly] Firefly session appeared, but cookie readiness was not confirmed", flush=True)
                return False
            if verification_state is not None:
                verification_state["done"] = True
            return True
        if not _wait_until_page_not_verifying(verification_page, timeout=25000):
            print("[Firefly] code entry did not clear Adobe verification page; cookie export is blocked", flush=True)
            return False
        confirmed_page = _confirm_adobe_email_verification_completed(verification_page, context, timeout=30000)
        if confirmed_page is None:
            print("[Firefly] code cleared the verification page, but logged-in state was not confirmed", flush=True)
            return False
        if verification_state is not None:
            verification_state["done"] = True
        page = confirmed_page
        print("[Firefly] code accepted; returning to Firefly before cookie export", flush=True)
    else:
        return False

    _open_firefly_for_cookie_export(page, reason="email verification complete")
    return not _page_requires_adobe_email_verification(page)




































































def _click_action_button_by_text(page, patterns, timeout=2500):
    """Find a submit-like action button and try trusted mouse + JS fallbacks."""
    deadline = time.time() + max(timeout, 0) / 1000
    js_find_or_click = """({ patterns, click }) => {
        const regexes = patterns.map((p) => new RegExp(p, 'i'));
        const social = /continue\\s+with|google|facebook|apple|microsoft|line|phone/i;
        const collect = (root, out = []) => {
          if (!root || !root.querySelectorAll) return out;
          for (const node of root.querySelectorAll('*')) {
            out.push(node);
            if (node.shadowRoot) collect(node.shadowRoot, out);
          }
          return out;
        };
        const textOf = (node) => (
          node.innerText
          || node.textContent
          || node.value
          || node.getAttribute('aria-label')
          || node.getAttribute('title')
          || ''
        ).trim();
        const buttonish = (node) => {
          const tag = String(node.tagName || '').toLowerCase();
          const role = String(node.getAttribute && node.getAttribute('role') || '').toLowerCase();
          const type = String(node.type || node.getAttribute && node.getAttribute('type') || '').toLowerCase();
          return tag === 'button'
            || role === 'button'
            || tag === 'sp-button'
            || tag === 'coral-button'
            || (tag === 'input' && ['submit', 'button'].includes(type));
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
        const enabled = (node) => {
          if (!node) return false;
          const aria = String(node.getAttribute && node.getAttribute('aria-disabled') || '').toLowerCase();
          return !node.disabled && aria !== 'true' && !node.hasAttribute?.('disabled');
        };
        const nodes = collect(document);
        const candidates = [];
        for (const node of nodes) {
          if (!buttonish(node) || !visible(node) || !enabled(node)) continue;
          const text = textOf(node);
          const isSubmit = String(node.type || '').toLowerCase() === 'submit';
          const matched = regexes.some((re) => re.test(text));
          if (!matched && !isSubmit) continue;
          if (social.test(text)) continue;
          const box = node.getBoundingClientRect();
          let score = matched ? 1000 : 0;
          if (isSubmit) score += 150;
          score += Math.max(0, box.top) / 10;
          score += Math.max(0, box.left) / 1000;
          candidates.push({ node, text, box, score });
        }
        if (!candidates.length) return { found: false, reason: 'no-button' };
        candidates.sort((a, b) => b.score - a.score || b.box.top - a.box.top || b.box.left - a.box.left);
        const target = candidates[0].node;

        target.scrollIntoView({ block: 'center', inline: 'center' });
        for (let parent = target.parentElement; parent; parent = parent.parentElement) {
          const style = getComputedStyle(parent);
          const canScroll = /(auto|scroll)/i.test(style.overflowY || '')
            && parent.scrollHeight > parent.clientHeight;
          if (!canScroll) continue;
          const box = target.getBoundingClientRect();
          const pbox = parent.getBoundingClientRect();
          if (box.top < pbox.top || box.bottom > pbox.bottom) {
            parent.scrollTop += box.top - pbox.top - Math.max(8, (parent.clientHeight - box.height) / 2);
          }
        }

        const box = target.getBoundingClientRect();
        const x = Math.min(Math.max(box.left + box.width / 2, 6), innerWidth - 6);
        const y = Math.min(Math.max(box.top + box.height / 2, 6), innerHeight - 6);
        const topNode = document.elementFromPoint(x, y);
        if (click) {
          try { target.focus({ preventScroll: true }); } catch (_) {}
          const Pointer = window.PointerEvent || window.MouseEvent;
          for (const name of ['pointerover', 'mouseover', 'pointerdown', 'mousedown', 'pointerup', 'mouseup']) {
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
        }
        return {
          found: true,
          clicked: Boolean(click),
          text: textOf(target),
          x,
          y,
          topText: topNode ? textOf(topNode).slice(0, 80) : '',
          topTag: topNode ? String(topNode.tagName || '') : '',
        };
    }"""
    while True:
        try:
            result = page.evaluate(js_find_or_click, {"patterns": list(patterns), "click": False})
            if result and result.get("found"):
                try:
                    page.mouse.move(float(result["x"]), float(result["y"]))
                    page.mouse.down()
                    page.mouse.up()
                    return True
                except Exception:
                    pass
                try:
                    result = page.evaluate(js_find_or_click, {"patterns": list(patterns), "click": True})
                    if result and result.get("clicked"):
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        if time.time() >= deadline:
            return False
        try:
            page.wait_for_timeout(250)
        except Exception:
            time.sleep(0.25)


def _press_continue(page):
    if _click_action_button_by_text(page, [
        r"^\s*Continue\s*$",
        r"^\s*Next\s*$",
        r"^\s*Create account\s*$",
        r"^\s*Sign up\s*$",
        r"^\s*Agree and continue\s*$",
        r"^\s*继续\s*$",
        r"^\s*下一步\s*$",
        r"^\s*创建帐户\s*$",
        r"^\s*创建账户\s*$",
        r"^\s*注册\s*$",
        r"^\s*同意并继续\s*$",
    ], timeout=2500):
        return True
    candidates = [
        r"^\s*Continue\s*$", r"^\s*Next\s*$", r"^\s*Create account\s*$",
        r"^\s*Sign up\s*$", r"^\s*Agree and continue\s*$",
        r"^\s*继续\s*$", r"^\s*下一步\s*$", r"^\s*创建帐户\s*$",
        r"^\s*创建账户\s*$", r"^\s*注册\s*$", r"^\s*同意并继续\s*$",
    ]
    for text in candidates:
        if _click_by_text(page, re.compile(text, re.I), timeout=2500):
            return True
    for selector in [
        "button[type='submit']",
        "input[type='submit']",
    ]:
        try:
            page.locator(selector).first.click(timeout=2500)
            return True
        except Exception:
            pass
    return False










def _save_debug_artifacts(page, tag, extra=None):
    ts = int(time.time())
    safe_tag = re.sub(r"[^a-zA-Z0-9_.-]+", "_", tag or "firefly")
    base = os.path.join(ARTIFACT_DIR, f"{safe_tag}_{ts}")
    full_debug = _config_bool("firefly_debug_artifacts", default=False)
    try:
        if full_debug:
            page.screenshot(path=f"{base}.png", full_page=True, timeout=10000)
    except Exception:
        pass
    try:
        with open(f"{base}.txt", "w", encoding="utf-8") as f:
            f.write(f"url={page.url}\n")
            f.write(f"title={page.title()}\n")
            if full_debug:
                f.write((page.locator("body").inner_text(timeout=3000) or "")[:5000])
                try:
                    debug = page.evaluate(
                        """() => ({
                            iframes: Array.from(document.querySelectorAll('iframe[src]')).map((node) => node.src),
                            iframeBoxes: Array.from(document.querySelectorAll('iframe')).map((node) => {
                              const box = node.getBoundingClientRect();
                              const style = getComputedStyle(node);
                              return {
                                src: node.getAttribute('src') || '',
                                currentSrc: node.src || '',
                                x: box.x,
                                y: box.y,
                                width: box.width,
                                height: box.height,
                                display: style.display,
                                visibility: style.visibility,
                                opacity: style.opacity,
                              };
                            }),
                            fcToken: document.querySelector('input[name="fc-token"], textarea[name="fc-token"]')?.value || '',
                            captchaNodes: Array.from(document.querySelectorAll('[data-sitekey], [data-pkey], [data-hcaptcha-sitekey], [data-recaptcha-sitekey]')).map((node) => ({
                              tag: node.tagName,
                              cls: node.className,
                              sitekey: node.getAttribute('data-sitekey') || node.getAttribute('data-pkey') || node.getAttribute('data-hcaptcha-sitekey') || node.getAttribute('data-recaptcha-sitekey') || '',
                            })),
                        })"""
                    )
                    f.write("\n\nCAPTCHA_DEBUG=" + json.dumps(debug, ensure_ascii=False)[:5000])
                except Exception:
                    pass
                try:
                    frames = [
                        {"url": frame.url, "name": frame.name}
                        for frame in page.frames
                    ]
                    f.write("\n\nPLAYWRIGHT_FRAMES=" + json.dumps(frames, ensure_ascii=False)[:5000])
                except Exception:
                    pass
                if extra is not None:
                    try:
                        f.write("\n\nEXTRA_DEBUG=" + json.dumps(extra, ensure_ascii=False, indent=2)[:20000])
                    except Exception:
                        f.write("\n\nEXTRA_DEBUG=" + str(extra)[:20000])
    except Exception:
        pass
    return base




def _keep_failed_browser():
    return _config_bool("firefly_keep_failed_browser", default=False)




# ============================================================================
# 浏览器指纹 / stealth profile（只改浏览器环境，不改注册业务流程）
# ============================================================================

_FIREFLY_VIEWPORT_PROFILES = [
    (1920, 1080, 1920, 1080),
    (1536, 864, 1536, 864),
    (1440, 900, 1440, 900),
    (1366, 768, 1366, 768),
    (1600, 900, 1600, 900),
    (1280, 800, 1280, 800),
]
_FIREFLY_FAST_VIEWPORT_PROFILES = [
    (1280, 620, 1366, 768),
    (1240, 620, 1366, 768),
    (1200, 620, 1280, 768),
]

_FIREFLY_GEO_PROFILES = {
    "us": ("America/Los_Angeles", "en-US", "en-US,en;q=0.9"),
    "ca": ("America/Toronto", "en-CA", "en-CA,en;q=0.9"),
    "gb": ("Europe/London", "en-GB", "en-GB,en;q=0.9"),
    "de": ("Europe/Berlin", "de-DE", "de-DE,de;q=0.9,en;q=0.7"),
    "fr": ("Europe/Paris", "fr-FR", "fr-FR,fr;q=0.9,en;q=0.7"),
    "jp": ("Asia/Tokyo", "ja-JP", "ja-JP,ja;q=0.9,en;q=0.7"),
    "sg": ("Asia/Singapore", "en-SG", "en-SG,en;q=0.9"),
    "hk": ("Asia/Hong_Kong", "zh-HK", "zh-HK,zh;q=0.9,en;q=0.7"),
    "tw": ("Asia/Taipei", "zh-TW", "zh-TW,zh;q=0.9,en;q=0.7"),
    "au": ("Australia/Sydney", "en-AU", "en-AU,en;q=0.9"),
    "nl": ("Europe/Amsterdam", "nl-NL", "nl-NL,nl;q=0.9,en;q=0.7"),
}
_FIREFLY_DEFAULT_GEO = ("America/Los_Angeles", "en-US", "en-US,en;q=0.9")

_FIREFLY_PROXY_GEO_CACHE = {}
_FIREFLY_PROXY_GEO_LOCK = threading.Lock()
_FIREFLY_COUNTRY_ALIASES = {
    "usa": "us",
    "unitedstates": "us",
    "united_states": "us",
    "uk": "gb",
    "unitedkingdom": "gb",
    "united_kingdom": "gb",
}

_FIREFLY_PROFILE_DIR = os.path.join(BASE_DIR, "firefly_browser_profiles")
try:
    os.makedirs(_FIREFLY_PROFILE_DIR, exist_ok=True)
except Exception:
    pass
















def _bundled_chromium_executable():
    browser_root = os.path.join(BASE_DIR, "ms-playwright")
    if not os.path.isdir(browser_root):
        return ""
    try:
        names = sorted(
            os.listdir(browser_root),
            key=lambda name: (
                0 if name.startswith("chromium-") else 1,
                name,
            ),
            reverse=True,
        )
    except Exception:
        return ""
    candidates = []
    for name in names:
        if not name.startswith("chromium-"):
            continue
        candidates.append(os.path.join(browser_root, name, "chrome-win64", "chrome.exe"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return ""


def _bundled_headless_shell_executable():
    browser_root = os.path.join(BASE_DIR, "ms-playwright")
    if not os.path.isdir(browser_root):
        return ""
    try:
        names = sorted(
            os.listdir(browser_root),
            key=lambda name: (
                0 if name.startswith("chromium_headless_shell-") else 1,
                name,
            ),
            reverse=True,
        )
    except Exception:
        return ""
    for name in names:
        if not name.startswith("chromium_headless_shell-"):
            continue
        path = os.path.join(browser_root, name, "chrome-headless-shell-win64", "chrome-headless-shell.exe")
        if os.path.isfile(path):
            return path
    return ""


def _resolve_chrome_executable():
    cfg = _load_config()
    candidates = [
        os.environ.get("FIREFLY_CHROME_PATH"),
        os.environ.get("ADMIN_CHROME_PATH"),
        os.environ.get("CHROME_PATH"),
        os.environ.get("GOOGLE_CHROME_SHIM"),
        cfg.get("chrome_executable_path"),
        cfg.get("chrome_path"),
        cfg.get("browser_executable_path"),
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


def _launch_browser(playwright, proxy=None, headless=False):
    common_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    headless_args = common_args + [
        "--disable-dev-shm-usage",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-sync",
        "--metrics-recording-only",
        "--mute-audio",
        "--no-sandbox",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--disable-ipc-flooding-protection",
        "--disable-hang-monitor",
        "--disable-prompt-on-repost",
        "--disable-domain-reliability",
        "--disable-client-side-phishing-detection",
        "--password-store=basic",
        "--use-mock-keychain",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--disable-accelerated-2d-canvas",
    ]
    args = headless_args if headless else common_args

    launch_args = {"headless": headless, "args": args}
    proxy_config = _playwright_proxy_config(proxy)
    if proxy_config:
        launch_args["proxy"] = proxy_config
    if headless:
        bundled_headless = _bundled_headless_shell_executable()
        if bundled_headless:
            try:
                br = playwright.chromium.launch(executable_path=bundled_headless, **launch_args)
                print(f"[Browser] using bundled headless shell: {bundled_headless}", flush=True)
                return br
            except Exception as exc:
                print(f"[Browser] bundled headless shell 启动失败({str(exc)[:60]})，尝试内置 Chromium", flush=True)
    if not headless:
        chrome_path = _resolve_chrome_executable()
        if chrome_path:
            try:
                br = playwright.chromium.launch(executable_path=chrome_path, **launch_args)
                print(f"[Browser] using Chrome: {chrome_path}", flush=True)
                return br
            except Exception as exc:
                print(f"[Browser] 指定 Chrome 启动失败({str(exc)[:60]})，回退内置 Chromium", flush=True)
    bundled_chromium = _bundled_chromium_executable()
    if bundled_chromium:
        try:
            br = playwright.chromium.launch(executable_path=bundled_chromium, **launch_args)
            print(f"[Browser] using bundled Chromium: {bundled_chromium}", flush=True)
            return br
        except Exception as exc:
            print(f"[Browser] bundled Chromium 启动失败({str(exc)[:60]})，回退指定 Chrome", flush=True)
    chrome_path = _resolve_chrome_executable()
    if chrome_path:
        try:
            br = playwright.chromium.launch(executable_path=chrome_path, **launch_args)
            print(f"[Browser] using Chrome: {chrome_path}", flush=True)
            return br
        except Exception as exc:
            print(f"[Browser] 指定 Chrome 启动失败({str(exc)[:60]})，回退系统 Chrome", flush=True)
    try:
        return playwright.chromium.launch(channel="chrome", **launch_args)
    except Exception as exc:
        print(f"[Browser] 系统 Chrome 启动失败({str(exc)[:60]})", flush=True)
        raise








