# -*- coding: utf-8 -*-
"""Adobe User Management API (UMAPI) 客户端：纯 HTTP 增删团队成员 + 发/收许可。
不碰浏览器、不怕反自动化、并发安全。每个管理员 org 需在 developer.adobe.com 建一个
Server-to-Server OAuth 项目(加 User Management API)拿 client_id / client_secret。
org_id 即 product_users_url 里的 xxxx@AdobeOrg。
"""
import json
import re
import time

import requests
from network_proxy import requests_proxies

IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
UMAPI_BASE = "https://usermanagement.adobe.io/v2/usermanagement"
SCOPE = "openid,AdobeID,user_management_sdk"
PROXIES = requests_proxies()

_TOKEN_CACHE = {}  # client_id -> (token, fetched_at)


def org_id_from_url(url):
    m = re.search(r"/([0-9A-Za-z]+@AdobeOrg)/", str(url or ""))
    return m.group(1) if m else ""


def get_token(client_id, client_secret):
    now = time.time()
    cached = _TOKEN_CACHE.get(client_id)
    if cached and now - cached[1] < 1200:   # 缓存 20 分钟
        return cached[0]
    r = requests.post(IMS_TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": SCOPE,
    }, timeout=30, proxies=PROXIES)
    if r.status_code != 200:
        raise RuntimeError(f"取 token 失败 {r.status_code}: {r.text[:200]}")
    token = r.json().get("access_token")
    if not token:
        raise RuntimeError(f"token 响应无 access_token: {r.text[:200]}")
    _TOKEN_CACHE[client_id] = (token, now)
    return token


def _headers(client_id, token):
    return {"Authorization": f"Bearer {token}", "X-Api-Key": client_id, "Content-Type": "application/json"}


def _get(url, client_id, token):
    for _ in range(6):
        r = requests.get(url, headers=_headers(client_id, token), timeout=60, proxies=PROXIES)
        if r.status_code == 429:
            time.sleep(min(int(r.headers.get("Retry-After", 10) or 10), 30))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"GET {url[-60:]} -> {r.status_code}: {r.text[:200]}")
        return r.json()
    raise RuntimeError("UMAPI 限流(429)重试过多")


def _post_action(org_id, client_id, token, actions):
    url = f"{UMAPI_BASE}/action/{org_id}"
    for _ in range(6):
        r = requests.post(url, headers=_headers(client_id, token), data=json.dumps(actions), timeout=60, proxies=PROXIES)
        if r.status_code == 429:
            time.sleep(min(int(r.headers.get("Retry-After", 10) or 10), 30))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"action -> {r.status_code}: {r.text[:300]}")
        return r.json()
    raise RuntimeError("UMAPI 限流(429)重试过多")


def list_product_profiles(org_id, client_id, token):
    """列出所有 group / product profile（用于确定加许可用的确切名字）。"""
    out, page = [], 0
    while True:
        j = _get(f"{UMAPI_BASE}/groups/{org_id}/{page}", client_id, token)
        for g in j.get("groups", []):
            out.append({
                "name": g.get("groupName") or g.get("name") or "",
                "type": g.get("type", ""),
                "productName": g.get("productName", ""),
                "memberCount": g.get("memberCount", g.get("userCount", "")),
            })
        if j.get("lastPage", True):
            break
        page += 1
    return out


def list_group_users(org_id, client_id, token, group_name):
    """列出某个 product profile / group 下的用户邮箱（小写集合）。"""
    from urllib.parse import quote
    emails, page = set(), 0
    while True:
        j = _get(f"{UMAPI_BASE}/users/{org_id}/{page}/{quote(group_name)}", client_id, token)
        for u in j.get("users", []):
            e = str(u.get("email") or u.get("username") or "").strip().lower()
            if e:
                emails.add(e)
        if j.get("lastPage", True) or not j.get("users"):
            break
        page += 1
    return emails


def _do_product(op, profile):
    # 用 productConfiguration（product profile 名）发/收许可；多数 org 用这个键
    return {op: {"productConfiguration": [profile]}}


def add_users(org_id, client_id, token, emails, profile, batch=10):
    res = []
    for i in range(0, len(emails), batch):
        chunk = emails[i:i + batch]
        actions = [{"user": e, "do": [_do_product("add", profile)]} for e in chunk]
        res.append(_post_action(org_id, client_id, token, actions))
    return res


def remove_users(org_id, client_id, token, emails, profile, batch=10):
    res = []
    for i in range(0, len(emails), batch):
        chunk = emails[i:i + batch]
        actions = [{"user": e, "do": [_do_product("remove", profile)]} for e in chunk]
        res.append(_post_action(org_id, client_id, token, actions))
    return res


def test_connection(org_id, client_id, client_secret):
    """验证凭证：取 token + 列 product profiles，返回可读结果。"""
    token = get_token(client_id, client_secret)
    profiles = list_product_profiles(org_id, client_id, token)
    return {"ok": True, "org_id": org_id, "profiles": profiles}
