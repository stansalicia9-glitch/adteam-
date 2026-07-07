# -*- coding: utf-8 -*-
"""按 cookie 查 Firefly 积分/额度(只读探测,不回写 cookie、不顶登录态)。
这套=adobe2api 算"2090/4000"那套(cookie-replay 刷 token + 只读查积分),搬来本地按子号各查各的,
跟号在不在 adobe2api 库无关。刷 token 与 adobe2api 的"自动刷新保活"同一操作=续命不踢人;
查积分是只读 GET;且本模块刷出的 new_cookie 直接丢弃、不回写任何文件→零副作用。

用法(命令行自测):
  python _quota.py <cookie串>
  python _quota.py --email a@x.com           # 从 firefly_adobe2api_cookies.json 取该号 cookie
  python _quota.py --sample 3                 # 测 cookies.json 前 3 个子号
"""
import sys, io, os, json, base64, uuid, time

if __name__ == "__main__":  # ★只命令行自测时改 stdout;被 import 时【绝不】改——否则会 GC 掉调用方(如 admin_jil_swap)的 stdout wrapper、关闭其 buffer → 收尾 print 报 "I/O operation on closed file"
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

import requests
try:
    import urllib3
    urllib3.disable_warnings()
except Exception:
    pass

_TRIAL_REFRESH_URL = "https://adobeid-na1.services.adobe.com/ims/check/v6/token"
_TRIAL_BKS_URL = "https://bks.adobe.io/v2/credits/cost"
_TRIAL_CREDITS_URL = "https://firefly.adobe.io/v1/credits/balance"
_TRIAL_API_KEY = "SunbreakWebUI1"
_TRIAL_CLIENT_ID = "clio-playground-web"
_TRIAL_SCOPE = (
    "AdobeID,firefly_api,openid,pps.read,pps.write,additional_info.projectedProductContext,"
    "additional_info.ownerOrg,uds_read,uds_write,ab.manage,read_organizations,"
    "additional_info.roles,account_cluster.read,creative_production,profile"
)
_TRIAL_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_TRIAL_FREE_FEATURES = [
    ("nano-flash", "firefly_3p:external:nano_banana_3", {}),
    ("veo3.1", "firefly_3p:external:veo_3", {"videoDuration": "5.0"}),
    ("veo-fast", "firefly_3p:external:veo_3_fast", {"videoDuration": "5.0"}),
    ("kling", "firefly_3p:external:kling_v3_ti2v", {"videoDuration": "5.0"}),
]


def _refresh_to_token(cookie, proxies):
    """裸 cookie -> access_token。返回 (token, reason)。new_cookie 故意丢弃=不回写、不动登录态。
    瞬时网络错误重试 3 次(减少 refresh_error 假失败)。"""
    r = None
    last = "refresh_error:unknown"
    for _att in range(3):
        try:
            r = requests.post(
                _TRIAL_REFRESH_URL,
                data={"client_id": _TRIAL_CLIENT_ID, "guest_allowed": "true", "scope": _TRIAL_SCOPE},
                headers={
                    "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                    "Cookie": cookie, "Origin": "https://firefly.adobe.com",
                    "Referer": "https://firefly.adobe.com/", "User-Agent": _TRIAL_UA,
                    "Connection": "close",
                },
                timeout=25, proxies=proxies, verify=False,
            )
            break
        except Exception as e:
            last = "refresh_error:%s" % str(e)[:60]
            time.sleep(1.2)
    if r is None:
        return None, last
    try:
        j = r.json()
    except Exception:
        j = {}
    tok = j.get("access_token")
    if tok:
        return tok, "ok"
    err = str(j.get("error") or j.get("error_description") or "").strip()
    return None, ("cookie_dead:HTTP%s %s" % (r.status_code, err[:40])).strip()


def _jwt_account_id(tok):
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        payload = json.loads(base64.urlsafe_b64decode(p).decode("utf-8", "ignore"))
        return str(payload.get("user_id") or payload.get("sub") or "").strip()
    except Exception:
        return ""


def _fetch_credits(tok, proxies):
    """返回 quota dict(含 available/total),只读 GET。瞬时失败/限流(429/5xx)退避重试3次;
    401/403/404=权益/账号问题重试无意义直接空。"""
    aid = _jwt_account_id(tok)
    if not aid:
        return {}
    hdr = {"Authorization": "Bearer %s" % tok, "x-api-key": _TRIAL_API_KEY,
           "x-account-id": aid, "Accept": "application/json", "Connection": "close"}
    for _ in range(3):
        try:
            r = requests.get(_TRIAL_CREDITS_URL, headers=hdr, timeout=15, proxies=proxies, verify=False)
            if r.status_code == 200:
                return (r.json().get("total") or {}).get("quota") or {}
            if r.status_code in (401, 403, 404):
                return {}
            time.sleep(0.8)
        except Exception:
            time.sleep(0.8)
    return {}


def _bks_state(tok, feature, md, proxies, retries=1):
    for _ in range(retries + 1):
        try:
            r = requests.post(
                _TRIAL_BKS_URL,
                headers={"Authorization": "Bearer %s" % tok, "X-API-Key": _TRIAL_API_KEY,
                         "X-Request-Id": uuid.uuid4().hex, "Accept": "application/json",
                         "Content-Type": "application/json", "Connection": "close"},
                json={"features": {feature: 1}, "metadata": dict(md)},
                timeout=20, proxies=proxies, verify=False,
            )
        except Exception:
            time.sleep(1); continue
        body = (r.text or "").lower()
        if r.status_code == 422 and "limited_taste" in body:
            return "free"
        if 200 <= r.status_code < 300:
            return "paid"
        if r.status_code == 403:
            return "no_entitlement"
        if r.status_code == 401:
            return "auth_failed"
        if r.status_code >= 500:
            time.sleep(1); continue
        return "E%s" % r.status_code
    return "query_error"


def query_quota(cookie_header, proxy=None, with_models=False):
    """轻量快查:刷 token + 只读查积分。返回 {token_ok, available, total, reason, models?}。
    with_models=True 才额外探 4 个免费模型(慢、多 ~4-8 次 HTTP)。"""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    out = {"token_ok": False, "available": None, "total": None, "reason": "", "models": None}
    if not cookie_header:
        out["reason"] = "no_cookie"; return out
    tok, note = _refresh_to_token(cookie_header, proxies)
    if not tok:
        out["reason"] = note; return out   # cookie_dead / refresh_error
    out["token_ok"] = True
    q = _fetch_credits(tok, proxies)
    if isinstance(q, dict):
        out["available"] = q.get("available")
        out["total"] = q.get("total") if q.get("total") is not None else q.get("cap")
    if with_models:
        out["models"] = {label: _bks_state(tok, feat, md, proxies) for label, feat, md in _TRIAL_FREE_FEATURES}
    out["reason"] = "ok"
    return out


# ----------------- 命令行自测 -----------------
def _load_cookie_for_email(email):
    base = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(base, "firefly_adobe2api_cookies.json")
    d = json.load(open(p, encoding="utf-8"))
    items = d.get("items", d) if isinstance(d, dict) else d
    for it in items:
        if str(it.get("name") or "").strip().lower() == email.strip().lower():
            return it.get("cookie") or ""
    return ""


def _iter_cookies(limit):
    base = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(base, "firefly_adobe2api_cookies.json")
    d = json.load(open(p, encoding="utf-8"))
    items = d.get("items", d) if isinstance(d, dict) else d
    for it in items[:limit]:
        yield str(it.get("name") or ""), (it.get("cookie") or "")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print("用法: python _quota.py <cookie> | --email a@x.com | --sample N"); sys.exit(1)
    if args[0] == "--email":
        em = args[1]
        ck = _load_cookie_for_email(em)
        print("%s ->" % em, json.dumps(query_quota(ck, with_models=("--models" in args)), ensure_ascii=False))
    elif args[0] == "--sample":
        n = int(args[1]) if len(args) > 1 else 3
        for em, ck in _iter_cookies(n):
            t0 = time.time()
            r = query_quota(ck, with_models=("--models" in args))
            print("%-45s %.1fs %s" % (em, time.time() - t0, json.dumps(r, ensure_ascii=False)))
    else:
        print(json.dumps(query_quota(args[0], with_models=("--models" in args)), ensure_ascii=False))
