# -*- coding: utf-8 -*-
"""Adobe Admin Console 内部 JIL 接口客户端（Teams 适用，绕开被挡的 UI 弹窗）。
用管理员会话 token(用户自己从浏览器 Copy-as-cURL 抓的 Bearer)直接调 bps-il.adobe.io 批量增删用户。
token 约 24h 过期，过期后用户重新抓一次粘进配置即可。无需 Developer Console / UMAPI。
经实测：加人请求只需 Authorization + X-Api-Key:ONESIE1 + x-jil-feature，无需指纹头。
"""
import json
import re
import threading
import time

import requests
from network_proxy import requests_proxies

BASE = "https://bps-il.adobe.io/jil-api/v2/organizations"
API_KEY = "ONESIE1"   # Admin Console 固定公开 client_id
PROXIES = requests_proxies()
_SESSION = requests.Session()
_tls = threading.local()   # 每线程一个"当前母号代理",并发换号各走各的住宅 IP


def set_console_proxy(proxy):
    """本线程后续 JIL 请求走该母号专属代理(每母号操作前调一次 → 一母号一固定住宅 IP,
    避免"同一 IP 操作一堆母号"被 Adobe 批量封)。proxy 为空=回退全局。"""
    _tls.proxies = ({"http": proxy, "https": proxy} if proxy else None)


def _cur_proxies():
    v = getattr(_tls, "proxies", "__unset__")
    return PROXIES if v == "__unset__" else v


def org_id_from_url(url):
    m = re.search(r"/([0-9A-Za-z]+@AdobeOrg)/", str(url or ""))
    return m.group(1) if m else ""


def product_id_from_url(url):
    m = re.search(r"/products/([0-9A-Za-z]+)/", str(url or ""))
    return m.group(1) if m else ""


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en_US",
        "X-Api-Key": API_KEY,
        "Origin": "https://adminconsole.adobe.com",
        "Referer": "https://adminconsole.adobe.com/",
        "x-jil-feature": "use_clam,pa_4280",
    }


def _json(r):
    try:
        return r.json()
    except Exception:
        return r.text[:300]


def _request(method, url, **kwargs):
    kwargs.setdefault("timeout", 60)
    kwargs.setdefault("proxies", _cur_proxies())
    last = None
    for attempt in range(4):
        try:
            return _SESSION.request(method, url, **kwargs)
        except requests.RequestException as exc:
            last = exc
            if attempt >= 3:
                raise
            print(f"[JIL] 请求异常，{attempt + 1}/4 重试: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
            time.sleep(1.5 * (attempt + 1))
    raise last


def get_license_groups(org_id, product_id, token):
    """列产品的 license groups(profile)，返回 [{id,name}]。"""
    url = f"{BASE}/{org_id}/products/{product_id}/license-groups?page=0&page_size=50"
    r = _request("GET", url, headers=_headers(token), timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"取 license-groups 失败 {r.status_code}: {r.text[:200]}")
    data = r.json()
    groups = data.get("licenseGroups", data) if isinstance(data, dict) else data
    out = []
    for g in (groups or []):
        if isinstance(g, dict) and g.get("id") is not None:
            out.append({"id": str(g.get("id")), "name": g.get("name", "")})
    return out


def list_product_users(org_id, product_id, token, page_size=100):
    """列该产品当前用户 [{email, id}]。分页拉全。删人要用内部 id(不是邮箱)。"""
    out, page = [], 0
    while True:
        url = f"{BASE}/{org_id}/products/{product_id}/users?page={page}&page_size={page_size}"
        r = _request("GET", url, headers=_headers(token), timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"列产品用户失败 {r.status_code}: {r.text[:200]}")
        data = r.json()
        users = data.get("users", data) if isinstance(data, dict) else data
        if not users:
            break
        for u in users:
            e = str((u or {}).get("email") or (u or {}).get("username") or "").strip()
            uid = str((u or {}).get("id") or "").strip()
            if e:
                out.append({"email": e, "id": uid})
        # ★分页收尾:优先看本页是否满 page_size(满了几乎肯定还有下一页);X-Page-Count 只作额外上界。
        #   之前"header缺失默认1→page(1)>=1直接break"会漏拉刚好满100条的org的后续页(漏删幽灵/席位算错)。
        try:
            total_pages = int(r.headers.get("X-Page-Count", "0") or "0")
        except Exception:
            total_pages = 0
        page += 1
        if len(users) < page_size or (total_pages and page >= total_pages):
            break
    return out


def _extract_qty(x):
    """从产品对象里尽量抠出总席位数（字段名不固定，多候选 + 嵌套兜底）。"""
    # ★真·总席位在 licenseQuantities:[{quantity,status,endDate}](购买总量)。优先读它——
    #   累加非过期条目的 quantity。provisionedQuantity 只是"已发放/已用"数(如 10 席只用 1 个
    #   会读成 1 → 拉子号算出可用 0、一个都不加),不能当总席位。
    lqs = x.get("licenseQuantities")
    if isinstance(lqs, list) and lqs:
        total = 0
        for lq in lqs:
            if not isinstance(lq, dict):
                continue
            if str(lq.get("status") or "").upper() in ("EXPIRED", "CANCELLED", "TERMINATED"):
                continue
            try:
                total += int(str(lq.get("quantity") or "0").strip())
            except Exception:
                pass
        if total > 0:
            return total
    for k in ("provisionedQuantity", "grantedQuantity", "totalQuantity", "assignableQuantity",
              "totalLicenseCount", "licenseCount", "quantity", "seats"):
        v = x.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    for sub in ("provisioning", "licenseTuple", "fulfillment"):
        d = x.get(sub)
        if isinstance(d, dict):
            for k in ("quantity", "grantedQuantity", "totalQuantity", "provisionedQuantity"):
                v = d.get(k)
                if isinstance(v, (int, float)) and v > 0:
                    return int(v)
    return 0


def list_organizations(token):
    """列该 token(母号)管理的组织 [{id(=org_id @AdobeOrg), name, country}]。
    协议登录拿 token 后用它发现 org_id(access_token 里没有 org_id)。"""
    url = "https://bps-il.adobe.io/jil-api/v2/organizations?page=0&page_size=20"
    r = _request("GET", url, headers=_headers(token), timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"列组织失败 {r.status_code}: {r.text[:200]}")
    data = r.json()
    orgs = data if isinstance(data, list) else (data.get("organizations") or [])
    out = []
    for o in (orgs or []):
        if isinstance(o, dict) and o.get("id"):
            out.append({"id": str(o["id"]), "name": o.get("name", ""), "country": o.get("countryCode", "")})
    return out


def list_products(org_id, token, page_size=100):
    """列组织所有产品 [{id, name, total}]，用于识别 Creative Cloud 产品、拼 users 地址、读总席位。"""
    out, page = [], 0
    while True:
        url = f"{BASE}/{org_id}/products?page={page}&page_size={page_size}"
        r = _request("GET", url, headers=_headers(token), timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"列产品失败 {r.status_code}: {r.text[:200]}")
        data = r.json()
        prods = data.get("products", data) if isinstance(data, dict) else data
        if not prods:
            break
        for x in (prods or []):
            if not isinstance(x, dict):
                continue
            pid = str(x.get("id") or "").strip()
            if not pid:
                continue
            name = (x.get("longName") or x.get("name") or x.get("longNameForUI")
                    or x.get("productName") or x.get("displayName") or "")
            out.append({"id": pid, "name": name, "total": _extract_qty(x)})
        try:
            total_pages = int(r.headers.get("X-Page-Count", "0") or "0")
        except Exception:
            total_pages = 0
        page += 1
        if len(prods) < page_size or (total_pages and page >= total_pages):
            break
    return out


def get_product_seats(org_id, product_id, token):
    """读某产品的总席位数（扫不到字段返回 0）。"""
    for x in list_products(org_id, token):
        if x["id"] == str(product_id):
            return x.get("total") or 0
    return 0


def list_org_users(org_id, token, page_size=100):
    """列【组织全体】用户 [{email, id}](含没有任何产品的"幽灵"号)。分页拉全。
    删加要基于这个来清,才能把历史遗留的无产品幽灵一起删掉(只看产品用户会漏)。"""
    out, page = [], 0
    while True:
        url = f"{BASE}/{org_id}/users?page={page}&page_size={page_size}"
        r = _request("GET", url, headers=_headers(token), timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"列组织用户失败 {r.status_code}: {r.text[:200]}")
        data = r.json()
        users = data.get("users", data) if isinstance(data, dict) else data
        if not users:
            break
        for u in users:
            e = str((u or {}).get("email") or (u or {}).get("username") or "").strip()
            uid = str((u or {}).get("id") or "").strip()
            if e:
                out.append({"email": e, "id": uid})
        try:
            total_pages = int(r.headers.get("X-Page-Count", "0") or "0")
        except Exception:
            total_pages = 0
        page += 1
        if len(users) < page_size or (total_pages and page >= total_pages):
            break
    return out


def remove_users(org_id, product_id, lg_id, token, user_ids, batch=10):
    """把用户【整个从组织删除】(按内部 userId)。PATCH org/users,op=remove path=/{uid}。

    ★修复"删加不删"BUG:旧实现是 path=/{uid}/products/{pid}/licenseGroups/{lg}——只剥掉产品许可,
    用户本人还赖在组织里(丢了产品的"幽灵"),每次删加越积越多(实测一个 org 积到 18 个幽灵)。
    删加/换号都要求"把旧子号彻底清掉",故改成 org 级删除用户。product_id/lg_id 保留仅为兼容签名。"""
    url = f"{BASE}/{org_id}/users"
    results = []
    for i in range(0, len(user_ids), batch):
        chunk = user_ids[i:i + batch]
        ops = [{"op": "remove", "path": f"/{uid}"} for uid in chunk if uid]
        if not ops:
            continue
        r = _request("PATCH", url, headers=_headers(token), data=json.dumps(ops), timeout=90)
        results.append({"status": r.status_code, "body": _json(r), "user_ids": chunk})
    return results


def add_users(org_id, product_id, lg_id, token, emails, batch=10):
    """批量加用户到产品(发许可)。POST users:batch。返回每批 (status, body)。"""
    url = f"{BASE}/{org_id}/users%3Abatch"
    results = []
    for i in range(0, len(emails), batch):
        chunk = emails[i:i + batch]
        body = [{
            "email": e,
            "type": "TYPE2E",  # 实测 TYPE1/TYPE2E 都一样(新成员都要等Adobe下发entitlement),改不改无关,保持原样
            "products": [{"id": product_id, "licenseGroups": [{"id": str(lg_id)}]}],
            "roles": [],
            "userGroups": [],
        } for e in chunk]
        r = _request("POST", url, headers=_headers(token), data=json.dumps(body), timeout=90)
        results.append({"status": r.status_code, "body": _json(r), "emails": chunk})
    return results


def test_token(org_id, product_id, token):
    """只读验证 token：列 license-groups。成功说明 token 有效、能调接口。"""
    groups = get_license_groups(org_id, product_id, token)
    return {"ok": True, "org_id": org_id, "product_id": product_id, "license_groups": groups}
