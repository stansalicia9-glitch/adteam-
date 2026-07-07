# -*- coding: utf-8 -*-
"""Shared outbound proxy helpers."""
import hashlib
import json
import os
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ADMIN_CONFIG = os.path.join(BASE_DIR, "admin_console_config.json")
APP_CONFIG = os.path.join(BASE_DIR, "config.json")


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def normalize_proxy_url(proxy):
    text = str(proxy or "").strip()
    if not text:
        return ""
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


def configured_proxy():
    for value in (
        os.environ.get("PROXY"),
        os.environ.get("HTTPS_PROXY"),
        os.environ.get("HTTP_PROXY"),
        os.environ.get("ALL_PROXY"),
        _load_json(ADMIN_CONFIG).get("proxy"),
        _load_json(APP_CONFIG).get("proxy"),
    ):
        proxy = normalize_proxy_url(value)
        if proxy:
            return proxy
    return ""


def requests_proxies(proxy=None):
    proxy = normalize_proxy_url(proxy) or configured_proxy()
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


# ===== 每母号专属住宅 IP(防"同一 IP 操作一堆母号"被 Adobe 批量封) =====
def _sid_of(key):
    """任意 key(母号/子号 email) → 稳定 session id(同 key 同 session=同住宅 IP,不同 key 不同 IP)。"""
    k = str(key or "").strip().lower()
    if not k:
        return "0"
    return str(int(hashlib.md5(k.encode("utf-8")).hexdigest()[:8], 16))


def _residential_tpl():
    """住宅代理模板,含 {sid} 占位。配置在 config.json / admin_console_config.json 的 residential_proxy_tpl。
    例:http://USER668594-zone-custom-region-US-session-{sid}-sessTime-180-sessAuto-1:8b5bba@us.rrp.bestgo.work:10000"""
    return (str(_load_json(ADMIN_CONFIG).get("residential_proxy_tpl") or "").strip()
            or str(_load_json(APP_CONFIG).get("residential_proxy_tpl") or "").strip())


def proxy_for_id(key):
    """按任意 key(母号 email / 子号 email)→ 专属住宅代理 URL(同 key 同固定 IP、不同 key 不同 IP)。
    母号用它=每母号一固定 IP;子号导 cookie 用它=每子号一个不同 IP(避免一个 org 几百成员同 IP 批量登录)。
    没配住宅模板则回退全局 configured_proxy。"""
    tpl = _residential_tpl()
    if not tpl:
        return configured_proxy()
    try:
        return tpl.format(sid=_sid_of(key))
    except Exception:
        return configured_proxy()


def proxy_for_console(console):
    """该母号专属的住宅代理 URL(每母号一个固定住宅 IP)。"""
    return proxy_for_id((console or {}).get("admin_email") or (console or {}).get("name") or "")
