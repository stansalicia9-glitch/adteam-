# -*- coding: utf-8 -*-
"""母号安全处置:①协议改 Adobe 密码 + 全局登出(踢入侵者会话);②设备码刷新 refresh_token(手动改完MS密码后拿新RT)。
改 Adobe 密码走 passwordRecovery 接码(零密码,不需要旧密码);改 MS 密码微软卡自动化只能手动,改完用设备码重新拿RT。"""
import json
import os
import random
import string
import sys
import time
import uuid

import requests

import admin_login_protocol as alp
import admin_console_manage as acm

B = alp.B
JSL = alp.JSL
UA = alp.UA
STATE_HDR = alp.STATE_HDR
IDV_HDR = alp.IDV_HDR
DEFAULT_MS_CLIENT = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"


def gen_password():
    """强随机 Adobe 密码(含大小写+数字+特殊)。"""
    body = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(11))
    return "Ff" + body + "!7"


def _save_field(email, field, value):
    """把某母号的某字段写回 config(admin_password/admin_refresh_token 等 save_consoles_merge 不覆盖的字段)。"""
    d = json.load(open(acm.CONFIG_FILE, encoding="utf-8-sig"))
    for c in d.get("consoles", []):
        if (c.get("admin_email") or "").strip().lower() == email.strip().lower():
            c[field] = value
    with open(acm.CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)


def change_adobe_password(console, proxy=None, new_password=None, global_logout=True, log=print):
    """接码(passwordRecovery)改母号 Adobe 密码 + 可选全局登出(踢所有在线会话/入侵者)。零密码。
    返回 (ok, new_password_or_'', msg)。成功后新密码已写回 config 的 admin_password。"""
    email = (console.get("admin_email") or "").strip()
    rt = console.get("admin_refresh_token") or ""
    cid = console.get("admin_client_id") or DEFAULT_MS_CLIENT
    if not (email and rt):
        return False, "", "缺 admin_email/admin_refresh_token"
    newpw = new_password or gen_password()
    CID = "ONESIE1"
    s = requests.Session()
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    s.headers.update({"x-ims-clientid": CID, "content-type": "application/json",
                      "accept": "application/json, text/plain, */*", "accept-language": "en-US,en;q=0.9",
                      "origin": B, "referer": B + "/", "user-agent": UA})

    def _pull(r):
        if r.headers.get(STATE_HDR):
            s.headers[STATE_HDR] = r.headers[STATE_HDR]
        if r.headers.get(IDV_HDR):
            s.headers[IDV_HDR] = r.headers[IDV_HDR]

    def _req(m, p, **kw):
        r = s.request(m, p if p.startswith("http") else B + p, timeout=25, **kw)
        _pull(r)
        return r

    try:
        _req("GET", "/signin/v2/configurations/%s?jslVersion=%s" % (CID, JSL))
        _req("POST", "/signin/v2/users/accounts?jslVersion=" + JSL,
             data=json.dumps({"username": email, "usernameType": "EMAIL"}))
        b2 = {"extraPbaChecks": False, "pbaPolicy": None, "username": email, "usernameType": "EMAIL",
              "accountType": "individual", "deviceInfo": {"lsId": str(uuid.uuid4()), "hdId": None}}
        rs = _req("POST", "/signin/v2/authenticationstate?purpose=passwordRecovery&jslVersion=" + JSL, data=json.dumps(b2))
        if rs.status_code not in (200, 201):
            return False, "", "建认证态 %d(429=换IP重试)" % rs.status_code
        t0 = time.time()
        rsend = _req("POST", "/signin/v3/challenges?purpose=passwordRecovery&factor=email&extendedAuthState=false&jslVersion=" + JSL, data="{}")
        if rsend.status_code != 200:
            return False, "", "发码 %d" % rsend.status_code
        code = alp._read_adobe_code_graph(cid, rt, t0, log=log)
        if not code:
            return False, "", "Graph读不到验证码(RT失效?)"
        rtok = _req("POST", "/signin/v3/tokens?credential=code&jslVersion=" + JSL,
                    data=json.dumps({"purpose": "passwordRecovery", "code": code}))
        bearer = ""
        try:
            bearer = rtok.json().get("token") or ""
        except Exception:
            pass
        if not bearer:
            return False, "", "验码失败 %d" % rtok.status_code
        # PUT /v1/passwords {password, doGlobalLogout}
        hp = {k: v for k, v in s.headers.items()}
        hp["authorization"] = "Bearer " + bearer
        rp = s.put(B + "/signin/v1/passwords",
                   data=json.dumps({"password": newpw, "doGlobalLogout": bool(global_logout)}),
                   headers=hp, timeout=25)
        if rp.status_code in (200, 204):
            _save_field(email, "admin_password", newpw)
            log("[改密] ✅ %s Adobe密码已改%s,新密码=%s" % (email, "+全局登出(踢入侵者)" if global_logout else "", newpw))
            return True, newpw, ""
        return False, "", "PUT passwords %d %s" % (rp.status_code, (rp.text or "")[:80])
    except Exception as exc:
        return False, "", "异常 %s" % str(exc)[:80]


# ===== 全自动刷新 refresh_token(密码登录 MS → 授权 Graph → 拿新 RT,零手动)=====
_COOKIE_TOOL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                                "Cookie登录导出工具")


def _post_retry(url, data, proxies=None, tries=3):
    """token 端点直连+重试(住宅代理抖动时 token 交换常 RemoteDisconnected,直连更稳)。"""
    last = None
    for i in range(tries):
        try:
            return requests.post(url, data=data, proxies=proxies, timeout=25).json()
        except Exception as exc:
            last = exc
            time.sleep(1.2 * (i + 1))
    raise last


def refresh_rt_auto(console, proxy=None, log=print):
    """★全自动刷新 admin_refresh_token(零手动):用存的 MS 密码(admin_password_alt)纯协议登录微软建会话
    → 用会话授权 Graph 客户端拿 code → 换新 RT(带 Graph+offline_access)→ 写回 config。返回 (ok, msg)。
    用于:手动改完 MS 密码(旧RT作废)后一键恢复接码;或定期轮换 RT。需 Cookie登录导出工具/_ms_login_proto。"""
    email = (console.get("admin_email") or "").strip()
    ms_pw = console.get("admin_password_alt") or ""
    if not (email and ms_pw):
        return False, "缺 admin_email/admin_password_alt(MS邮箱密码)"
    try:
        if _COOKIE_TOOL_DIR not in sys.path:
            sys.path.insert(0, _COOKIE_TOOL_DIR)
        import _ms_login_proto as P
    except Exception as exc:
        return False, "缺 _ms_login_proto(Cookie登录导出工具)不在:%s" % str(exc)[:60]
    from urllib.parse import quote, urlparse, parse_qs
    acc = {"email": email, "pwd": ms_pw, "rec_email": console.get("admin_rec_email") or "",
           "rec_pwd": console.get("admin_rec_pwd") or "", "cid": console.get("admin_client_id"), "rt": ""}
    try:
        res = P.protocol_login(acc, proxy=proxy)
    except Exception as exc:
        return False, "MS登录异常 %s" % str(exc)[:70]
    s = res.get("session")
    if not s:
        return False, "MS登录没建会话(reason=%s;密码错/proofs墙?)" % res.get("reason")
    # consent 也没关系——只要 login.live.com 会话建好了,授权 Graph(微软预授权客户端)即可
    GID = DEFAULT_MS_CLIENT
    REDIR = "https://login.microsoftonline.com/common/oauth2/nativeclient"
    au = ("https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize?client_id=%s&response_type=code"
          "&redirect_uri=%s&scope=%s&response_mode=query"
          % (GID, quote(REDIR, safe=""), quote("https://graph.microsoft.com/.default offline_access")))
    try:
        r = s.get(au, allow_redirects=True, timeout=30)
    except Exception as exc:
        return False, "Graph授权异常 %s" % str(exc)[:60]
    code = ""
    import re as _re
    for u in [r.url] + [h.headers.get("location", "") for h in r.history]:
        v = parse_qs(urlparse(u).query).get("code", [""])[0]
        if v:
            code = v
            break
    if not code:
        m = _re.search(r'[?&]code=([^&"\'<>\s]+)', r.text or "")
        if m:
            code = m.group(1)
    if not code:
        return False, "没拿到授权code(会话可能没生效/需consent)"
    try:
        tk = _post_retry("https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
                         {"client_id": GID, "grant_type": "authorization_code", "code": code,
                          "redirect_uri": REDIR, "scope": "https://graph.microsoft.com/.default offline_access"})
    except Exception as exc:
        return False, "换RT连接失败 %s" % str(exc)[:60]
    newrt = tk.get("refresh_token") or ""
    if not newrt:
        return False, "换RT失败 %s" % (tk.get("error") or "")
    # 校验能读 Graph
    try:
        at = _post_retry("https://login.microsoftonline.com/common/oauth2/v2.0/token",
                         {"client_id": GID, "grant_type": "refresh_token", "refresh_token": newrt,
                          "scope": "https://graph.microsoft.com/.default"}).get("access_token", "")
    except Exception:
        at = ""
    if not at:
        return False, "新RT换不出Graph token(作废?)"
    _save_field(email, "admin_refresh_token", newrt)
    log("[换RT] ✅ %s 全自动拿到新RT(%d字)+Graph验证通过,已写回config" % (email, len(newrt)))
    return True, "RT已更新(%d字)" % len(newrt)


def _pick(consoles, sel):
    s = (sel or "").strip().lower()
    return [c for c in consoles if s == (c.get("admin_email") or "").strip().lower()
            or s == (c.get("name") or "").strip().lower()
            or (s and (s in (c.get("admin_email") or "").lower() or s in (c.get("name") or "").lower()))]


if __name__ == "__main__":
    import argparse
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    import network_proxy as _np
    ap = argparse.ArgumentParser(description="母号安全:协议改Adobe密码+全局登出 / 全自动换RT")
    ap.add_argument("--console", required=True, help="母号邮箱/名称(逗号分隔可多个;'all'=全部)")
    ap.add_argument("--action", choices=["change-password", "refresh-rt", "both"], required=True)
    args = ap.parse_args()
    cfg, consoles = acm._load_consoles()
    if args.console.strip().lower() == "all":
        targets = consoles
    else:
        targets = []
        for sel in args.console.split(","):
            targets += [c for c in _pick(consoles, sel) if c not in targets]
    if not targets:
        print("没找到母号", args.console)
        raise SystemExit(1)
    print("#### 安全处置 %d 个母号,动作=%s ####" % (len(targets), args.action), flush=True)
    okn = 0
    for c in targets:
        tag = c.get("admin_email") or c.get("name")
        proxy = _np.proxy_for_id(c.get("admin_email") or tag)
        print("=" * 56, flush=True)
        print("[%s] 处理中…" % tag, flush=True)
        good = True
        if args.action in ("change-password", "both"):
            ok, newpw, msg = change_adobe_password(c, proxy=proxy)
            print("[%s] 改Adobe密码+全局登出: %s" % (tag, ("✅ 新密码=" + newpw) if ok else ("❌ " + msg)), flush=True)
            good = good and ok
        if args.action in ("refresh-rt", "both"):
            ok, msg = refresh_rt_auto(c, proxy=proxy)
            print("[%s] 全自动换RT: %s" % (tag, ("✅ " + msg) if ok else ("❌ " + msg)), flush=True)
            good = good and ok
        okn += 1 if good else 0
    print("#" * 56, flush=True)
    print("完成:%d/%d 个母号成功" % (okn, len(targets)), flush=True)
    raise SystemExit(0 if okn else 1)
