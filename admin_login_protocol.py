# -*- coding: utf-8 -*-
"""母号 Adobe IMS 协议登录(纯 requests,不开浏览器),替代 admin_seed_login.py 的浏览器登录。
照 _PROTOCOL_admin_login.md 蓝图:邮箱→邮箱验证码→密码→选企业profile→拿 jil_token。
state 靠 x-ims-authentication-state-encrypted + x-identity-verification-token 请求头链式传。
首跑探测用:每步详细打印 status/state/body,定位最终 token 落点 + profile 选择细节。
"""
import io
import json
import sys
import time
import uuid
from urllib.parse import quote

if __name__ == "__main__":  # 只 CLI 时包 utf-8;被 import 时不动调用方 stdout(否则多次包装会关闭 buffer)
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import requests

B = "https://auth.services.adobe.com"
JSL = "v2-v0.31.0-2-g1e8a8a8"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
STATE_HDR = "x-ims-authentication-state-encrypted"
IDV_HDR = "x-identity-verification-token"
# filtered_profiles 的 filter(从抓包原样取)
PROFILE_FILTER = ("{\"fallbackToAA\":true};hasRole('ORG_ADMIN') or hasRole('STORAGE_ADMIN') or "
                  "hasRole('DEPLOYMENT_ADMIN') or hasRole('PRODUCT_ADMIN') or hasRole('PRODUCT_SUPPORT_ADMIN') or "
                  "hasRole('LICENSE_ADMIN') or hasRole('SUPPORT_ADMIN') or hasRole('USER_GROUP_ADMIN') or "
                  "hasRole('CONTRACT_ADMIN')")
# 母号 adminconsole(ONESIE1)的 scope;子号 Firefly(clio-playground-web)的 scope
ADMIN_SCOPE = ("openid,AdobeID,additional_info.projectedProductContext,read_organizations,read_members,"
               "read_countries_regions,additional_info.roles,adobeio_api,read_auth_src_domains,authSources.rwd,"
               "bis.read.pi,app_policies.read,app_policies.write,client.read,publisher.read,client.scopes.read,"
               "creative_cloud,service_principals.write,aps.read.app_merchandising,aps.eval_licensesforapps,ab.manage,"
               "aps.device_activation_mgmt,pps.read,ip_list_write_scope,ip_list_check_scope,jil.facs_role_read,"
               "jil.facs_role_write,ims_cai.orgPolicies.read,ims_cai.orgPolicies.write,security_profile.mfa_status.r")
FIREFLY_SCOPE = ("AdobeID,firefly_api,openid,pps.read,pps.write,additional_info.projectedProductContext,"
                 "additional_info.ownerOrg,uds_read,uds_write,ab.manage,read_organizations,"
                 "additional_info.roles,account_cluster.read,creative_production,profile")


class AdminLogin:
    def __init__(self, email, password, proxy=None, client_id="ONESIE1", scope=None, redirect_uri="https://adminconsole.adobe.com/"):
        self.email = email
        self.password = password
        self.client_id = client_id           # 母号=ONESIE1(adminconsole);子号=clio-playground-web(Firefly)
        self.scope = scope or ADMIN_SCOPE
        self.redirect_uri = redirect_uri      # 母号=adminconsole;子号=firefly.adobe.com
        self.s = requests.Session()
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        self.s.headers.update({
            "x-ims-clientid": client_id,
            "content-type": "application/json",
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-US,en;q=0.9",
            "origin": B,
            "referer": B + "/",
            "user-agent": UA,
        })

    def _pull_state(self, r):
        st = r.headers.get(STATE_HDR)
        idv = r.headers.get(IDV_HDR)
        if st:
            self.s.headers[STATE_HDR] = st
        if idv:
            self.s.headers[IDV_HDR] = idv

    def _req(self, method, path, tag, **kw):
        url = path if path.startswith("http") else (B + path)
        kw.setdefault("timeout", (15, 60))   # ★默认超时(连15s/读60s):防并发批量时某请求无响应把worker线程永久挂死→整个任务卡死
        r = self.s.request(method, url, **kw)
        self._pull_state(r)
        body = (r.text or "")[:400]
        print("[%s] %s %s | state=%s idv=%s | %s" % (
            tag, method, r.status_code,
            "Y" if r.headers.get(STATE_HDR) else "-",
            "Y" if r.headers.get(IDV_HDR) else "-",
            body), flush=True)
        return r

    def run(self, refresh_token, client_id, org_id, product_id, mail_code_fn=None, want="token", profile_filter=None, fed_token=None):
        # mail_code_fn: 注入拿码(子号用 cloudflare;不给则用 outlook refresh_token)。want: "token"=母号拿access_token / "cookie"=子号导出登录态cookie
        # 1) 登录配置(初始化 cookie/可能首个 state)
        self._req("GET", "/signin/v2/configurations/" + self.client_id + "?jslVersion=" + JSL, "configurations")
        # 2) 查号(提交邮箱)→ 回首个 state
        self._req("POST", "/signin/v2/users/accounts?jslVersion=" + JSL, "accounts",
                  data=json.dumps({"username": self.email, "usernameType": "EMAIL"}))
        # ★联合(federated)登录:有 fed_token 直接 credential=federated 换 susi(跳过 MFA+password;
        #   微软身份已由 headless 微软登录验过,Adobe 不再要邮箱 MFA)
        if fed_token:
            rpw = self._req("POST", "/signin/v2/tokens?credential=federated&jslVersion=" + JSL, "federated",
                            data=json.dumps({"token": fed_token, "accountType": "individual"}))
            susi = ""
            try:
                susi = rpw.json().get("token") or ""
            except Exception:
                pass
            print("[federated] SUSI token len=%d" % len(susi), flush=True)
            self.s.headers["authorization"] = "Bearer " + susi
        else:
            # 3) 建认证状态(MFA) —— body 必须含 username + usernameType + accountType(探测得:accountType 是关键)
            self._req("POST", "/signin/v2/authenticationstate?purpose=multiFactorAuthentication&jslVersion=" + JSL,
                      "auth-state", data=json.dumps({"username": self.email, "usernameType": "EMAIL", "accountType": "individual"}))
            # 4) 列因子 → 必须有 email(有时邮箱+手机一起出/或只出手机;我们只能拿邮箱码)
            rc = self._req("GET", "/signin/v3/challenges?purpose=multiFactorAuthentication&jslVersion=" + JSL, "challenges-list")
            try:
                factors = rc.json().get("availableFactors", []) or []
            except Exception:
                factors = []
            if "email" not in [str(f).lower() for f in factors]:
                print("❌ 该号无 email 验证因子(availableFactors=%s,只能手机/SMS),协议跳过(可走浏览器兜底/人工)" % factors, flush=True)
                return None
            if len(factors) > 1:
                print("[challenges] 多因子 %s → 选 email" % factors, flush=True)
            # 5) 发邮箱码(出邮箱+手机时也强制只走 email)
            t0 = time.time()
            self._req("POST", "/signin/v3/challenges?purpose=multiFactorAuthentication&factor=email&extendedAuthState=false&jslVersion=" + JSL,
                      "send-code", data="{}")
            # 6) 协议拿码(只认发码之后的新邮件)。母号=outlook refresh_token;子号=注入 mail_code_fn(cloudflare)
            print("[mail] 等邮箱新验证码…", flush=True)
            if mail_code_fn:
                code = mail_code_fn(t0)
            else:
                import firefly_register_yescaptcha as fr
                code, _link = fr._wait_for_outlook_adobe_email(refresh_token, client_id, timeout=120, fresh_after_ts=t0 - 10)
            if not code:
                print("❌ 没拿到验证码,中止", flush=True)
                return None
            print("[mail] 新验证码:", code, flush=True)
            # 7) 验码
            self._req("PUT", "/signin/v3/challenges?purpose=multiFactorAuthentication&jslVersion=" + JSL,
                      "verify-code", data=json.dumps({"code": code}))
            # 8) 验密码 → 拿 SUSI token
            rpw = self._req("POST", "/signin/v2/tokens?credential=password&jslVersion=" + JSL, "password",
                            data=json.dumps({"username": self.email, "usernameType": "EMAIL", "password": self.password,
                                             "accountType": "individual", "rememberMe": True}))
            susi = ""
            try:
                susi = rpw.json().get("token") or ""
            except Exception:
                pass
            print("[password] SUSI token len=%d" % len(susi), flush=True)
            # ★password 后:state/idv 头【保留】(filterprofilemapping 要带);只加 Bearer
            self.s.headers["authorization"] = "Bearer " + susi
        import base64 as _b64

        def _dec(t):
            p = t.split(".")[1]
            p += "=" * (-len(p) % 4)
            return json.loads(_b64.urlsafe_b64decode(p))
        # ★8.5) 子号激活(=网页"加入团队"):首次登录的子号有 pending 企业 profile,filtered_profiles 列不出,
        #   需 GET accounts/me 拿 pending link 的 linkId → POST /signin/v2/links/{linkId} {"status":"active"} 激活。
        #   激活后 filtered_profiles(preferForwardProfile)才返回企业 profile→选它→4000。
        if want == "cookie":
            try:
                me = self.s.get(B + "/signin/v1/accounts/me?client_id=" + self.client_id, timeout=20).json()
                links = ((me.get("profileData") or {}).get("links") or []) if isinstance(me, dict) else []
                acted = 0
                for lk in links:
                    if not isinstance(lk, dict):
                        continue
                    lid = lk.get("ident") or ""           # ★linkId 字段叫 ident
                    st = str(lk.get("status") or "").lower()
                    if lid and st != "active":             # invited/pending → 激活(=网页"加入团队")
                        ra = self.s.post(B + "/signin/v2/links/" + lid, data=json.dumps({"status": "active"}), timeout=20)
                        acted += 1
                        print("[加入团队激活] %s (%s) %s→active %s" % (lid[:16], lk.get("description"), st, ra.status_code), flush=True)
                if not acted:
                    print("[加入团队激活] profileData.links 无待激活项: %s" % json.dumps(links, ensure_ascii=False)[:300], flush=True)
            except Exception as _e:
                print("[加入团队激活] 异常: %s" % str(_e)[:90], flush=True)
        # 9) filtered_profiles → 找要选的 profile(母号=hasRole企业admin;子号=宽松filter列团队profile)
        pf = profile_filter or PROFILE_FILTER
        if profile_filter == "PROBE":  # 诊断:试多种 filter 看子号到底有没有企业 profile
            for _tf in ['{"fallbackToAA":true}', '{"fallbackToAA":false}', '',
                        "{\"fallbackToAA\":true};hasRole('PRODUCT_ADMIN') or hasRole('ORG_ADMIN') or hasRole('LICENSE_ADMIN')"]:
                try:
                    _r = self.s.get(B + "/signin/v2/accounts/filtered_profiles?filter=" + quote(_tf))
                    _ps = _r.json().get("filteredProfiles", [])
                    print("[probe %r] %d个: %s" % (_tf[:38] or "(空)", len(_ps),
                          [(p.get("description"), "linkId" if p.get("linkId") else "no-link") for p in _ps]), flush=True)
                except Exception as _e:
                    print("[probe err]", str(_e)[:60], flush=True)
            pf = '{"fallbackToAA":true}'
        rp = self.s.get(B + "/signin/v2/accounts/filtered_profiles?filter=" + quote(pf))
        profs = []
        try:
            profs = rp.json().get("filteredProfiles", [])
        except Exception:
            pass
        print("[profiles-all] %s | %s" % (rp.status_code, json.dumps(profs, ensure_ascii=False)[:500]), flush=True)
        # ★母号 vs 子号 profile 选择【相反】(2026-06-17 实测确认):
        if want == "token":
            # 母号(adminconsole/ONESIE1):用 hasRole filter,返回的就是 org-admin profile(母号个人AdobeID本身即admin,
            #   多数号 filtered_profiles 只返一个"Personal Account")→ ★必须选它(filterprofilemapping设上下文)token才带org-admin权限→JIL 200;
            #   不选则 JIL 403 Access-denied。所以母号直接选第一个(hasRole已过滤成admin)。
            ent = profs[0] if profs else None
            if not ent:  # hasRole 偶尔没返回 → 宽松filter重查,选母号自己profile(个人号即admin)
                try:
                    _rp2 = self.s.get(B + "/signin/v2/accounts/filtered_profiles?filter=" + quote('{"fallbackToAA":true}'))
                    _p2 = _rp2.json().get("filteredProfiles", [])
                    ent = _p2[0] if _p2 else None
                    if ent:
                        profs = _p2
                        print("[ent-profile] hasRole没返回,宽松filter重查到母号profile", flush=True)
                except Exception:
                    pass
        else:
            # 子号(Firefly/clio):宽松filter返 Personal + 企业member profile → ★必须选【企业】(有linkId非personal)→4000;
            #   选Personal只10积分(普号假成功)。选不到企业就 ent=None、绝不退选Personal,应重试等权益传播。
            ent = next((p for p in profs if p.get("linkId") and "personal" not in (p.get("description") or "").lower()), None)
            if not ent:
                ent = next((p for p in profs if p.get("linkId")), None)
        if not ent:
            print("[ent-profile] ⚠️ %s没选到profile(profs=%s)" % (
                "母号" if want == "token" else "子号(未被母号真正加入团队/权益未传播)",
                json.dumps([{"d": p.get("description"), "linkId": bool(p.get("linkId"))} for p in profs], ensure_ascii=False)), flush=True)
        print("[ent-profile] %s | %s" % (rp.status_code, json.dumps(ent, ensure_ascii=False) if ent else "无"), flush=True)
        guid = (ent or {}).get("userId")
        link_id = (ent or {}).get("linkId")
        # 10) 选 profile 设 filter-profile-map(母号选企业admin;子号选 personal;不选会 eoaChoose)
        if ent and guid:
            r1 = self.s.put(B + "/signin/v1/filterprofilemapping", data=json.dumps({"filter": pf, "guid": guid}))
            print("[filterprofilemapping] %s | %s" % (r1.status_code, (r1.text or "")[:80]), flush=True)
            if link_id:  # 有 linkId(企业/团队 profile)才 accounts/tokens;personal 无 linkId 跳过
                r2 = self.s.post(B + "/signin/v1/accounts/tokens", data=json.dumps({"linkId": link_id}))
                print("[accounts/tokens] %s | %s" % (r2.status_code, (r2.text or "")[:120]), flush=True)
                # ★2026-07-16 修 ims/tokens 401 invalid_token(子号全导成普号/导入403 的真因):
                #   SUSI/password token 绑的是【个人 AdobeID】(sub=…@AdobeID);accounts/tokens 用 linkId
                #   换回的才是【企业 profile 上下文】token(sub=…@…e)。ims/tokens 必须用企业 token,
                #   否则 Adobe 判 Invalid JWT→401→mid空→access_token空→cookie 只剩非鉴权项→误判普号。
                #   仅子号(want=cookie)flow 换头;母号(want=token)保持原逻辑不动,免回归。
                if want == "cookie":
                    try:
                        _ent_tok = r2.json().get("token") or ""
                    except Exception:
                        _ent_tok = ""
                    if _ent_tok:
                        self.s.headers["authorization"] = "Bearer " + _ent_tok
                        print("[ims/tokens] ★换用 accounts/tokens 企业 token(修 401 invalid_token→4000)", flush=True)
        else:
            print("[profile] 没有可选 profile", flush=True)
        r3 = self.s.post(B + "/signin/v1/ims/tokens", data=json.dumps({"rememberMe": True, "reauthenticate": None}))
        mid = ""
        try:
            mid = r3.json().get("token") or ""
        except Exception:
            pass
        print("[ims/tokens] %s mid_len=%d resp=%s" % (r3.status_code, len(mid), (r3.text or "")[:280]), flush=True)
        try:
            mp = _dec(mid)
            print("[mid payload]", json.dumps({k: mp.get(k) for k in ("aud", "sub", "scope", "as", "client_id", "pac", "pba")}, ensure_ascii=False), flush=True)
        except Exception as e:
            print("[mid dec err]", e, flush=True)
        # 11) B: 中间 token → 最终 access_token(ims-na1 隐式 authorize,302 带 #access_token)
        import re as _re
        from urllib.parse import unquote as _uq
        SCOPE = self.scope
        # ★B 真相(浏览器 hook 抓到):POST form 到 adobeid-na1.services.adobe.com/ims/fromSusi,token字段=中间token,
        #   该端点设 .services.adobe.com session cookie 并 302 链 → redirect_uri#access_token
        from urllib.parse import quote as _q
        redirect_uri = self.redirect_uri
        callback = "https://ims-na1.adobelogin.com/ims/adobeid/" + self.client_id + "/AdobeID/token?redirect_uri=" + _q(redirect_uri, safe="")
        form = {
            "remember_me": "true", "callback": callback, "client_id": self.client_id, "scope": SCOPE,
            "state": '{"jslibver":"%s","nonce":"9999"}' % JSL, "locale": "en_US",
            "flow_type": "token", "idp_flow_type": "login", "response_type": "token",
            "code_challenge_method": "plain", "redirect_uri": redirect_uri,
            "use_ms_for_expiry": "true", "flow": "signIn", "token": mid,
        }
        for k in ("authorization", STATE_HDR, IDV_HDR, "content-type"):
            self.s.headers.pop(k, None)
        access_token = ""

        def _find_at(resp):
            # access_token 可能在: 最终URL / 重定向Location / 响应HTML(fromSusi 返回 meta-refresh 跳转,token 在其 url 的 #access_token)
            for u in [resp.url] + [h.headers.get("location", "") for h in resp.history] + [resp.text or ""]:
                m = _re.search(r"access_token=([^&#\"'\s]+)", u or "")
                if m:
                    return _uq(m.group(1))
            return ""

        def _next_form(html):
            fm = _re.search(r'<form[^>]*action=["\']([^"\']+)["\'][^>]*>(.*?)</form>', html or "", _re.S | _re.I)
            if not fm:
                return None, None
            action = fm.group(1).replace("&amp;", "&")
            fields = {}
            for nm, val in _re.findall(r'<input[^>]*\bname=["\']([^"\']+)["\'][^>]*\bvalue=["\']([^"\']*)["\']', fm.group(2), _re.S | _re.I):
                fields[nm] = val.replace("&amp;", "&")
            return action, fields
        try:
            r = self.s.post("https://adobeid-na1.services.adobe.com/ims/fromSusi", data=form, allow_redirects=True, timeout=30)
            print("[fromSusi] %s cookies=%s" % (r.status_code, ",".join(sorted(set(self.s.cookies.keys())))), flush=True)
            access_token = _find_at(r)
            # fromSusi 返回 auto-submit HTML form 链,手动逐个跟(requests 不执行 JS)
            for _i in range(6):
                if access_token:
                    break
                action, fields = _next_form(r.text)
                if not action:
                    print("[form-chain] 无更多form; body=%s" % (r.text or "").replace("\n", " ").replace("\r", "")[:1600], flush=True)
                    break
                print("[form-chain] POST %s (%d fields)" % (action[:75], len(fields)), flush=True)
                r = self.s.post(action, data=fields, allow_redirects=True, timeout=25)
                access_token = _find_at(r)
        except Exception as e:
            print("[fromSusi] ERR %s" % str(e)[:90], flush=True)
        if not access_token:
            # fromSusi 已设 ims_sid/aux_sid → authorize(同 .services.adobe.com 域)现应能用 cookie 拿 token
            au2 = ("https://adobeid-na1.services.adobe.com/ims/authorize/v1?client_id=" + self.client_id + "&scope=" + quote(SCOPE) +
                   "&response_type=token&locale=en_US&jslVersion=" + JSL +
                   "&redirect_uri=" + quote(self.redirect_uri) +
                   "&state=" + quote('{"jslibver":"%s","nonce":"9999"}' % JSL))
            try:
                ra = self.s.get(au2, allow_redirects=False, timeout=20)
                loc = ra.headers.get("location", "")
                print("[authorize2] %s loc=%s" % (ra.status_code, loc[:130]), flush=True)
                m = _re.search(r"access_token=([^&#]+)", loc)
                if m:
                    access_token = _uq(m.group(1))
            except Exception as e:
                print("[authorize2] ERR %s" % str(e)[:80], flush=True)
        print("[★access_token] len=%d head=%s" % (len(access_token), access_token[:18]), flush=True)
        if access_token and org_id and product_id:
            try:
                jr = requests.get("https://bps-il.adobe.io/jil-api/v2/organizations/%s/products/%s/users?page=0&page_size=3" % (org_id, product_id),
                                  headers={"Authorization": "Bearer " + access_token, "X-Api-Key": "ONESIE1"}, timeout=20)
                print("[★★JIL with access_token] %s | %s" % (jr.status_code, (jr.text or "")[:140]), flush=True)
            except Exception as e:
                print("[JIL] ERR %s" % str(e)[:80], flush=True)
        if want == "cookie":
            # ★门禁:子号没选到【企业profile(有linkId)】= 权益没传播/被回收,此时 cookie 是 personal(10分废号)。
            #   绝不返回它——否则会被当"成功导出"写进 cookie 池、还覆盖掉已导好的企业4000 cookie(打穿"personal绝不卖")。
            if not (ent and link_id):
                print("[★cookie] ⚠️ 没有企业profile(personal/权益未传播),判失败不返回cookie(避免10分废号入池)", flush=True)
                return ""
            ck = "; ".join("%s=%s" % (c.name, c.value) for c in self.s.cookies)
            print("[★cookie] %d 个: %s" % (len(self.s.cookies), ",".join(sorted(set(c.name for c in self.s.cookies)))), flush=True)
            return ck
        return access_token


_GRAPH_TOKEN_EP = "https://login.microsoftonline.com/common/oauth2/v2.0/token"


def _graph_token(cid, rt):
    try:
        r = requests.post(_GRAPH_TOKEN_EP, data={"client_id": cid, "grant_type": "refresh_token",
                          "refresh_token": rt, "scope": "https://graph.microsoft.com/.default"}, timeout=25)
        return r.json().get("access_token", "") if r.status_code == 200 else ""
    except Exception:
        return ""


def _adobe_code_epoch(iso):
    from datetime import datetime, timezone
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return 0


def _read_adobe_code_graph(cid, rt, t0, timeout=120, log=print):
    """Graph 读 Adobe 单次验证码:只认发码时刻 t0 之后收到的最新一封(否则读到历史旧码→invalid)。"""
    import re as _re
    fresh = t0 - 15
    end = time.time() + timeout
    while time.time() < end:
        at = _graph_token(cid, rt)
        cand = []
        if at:
            for folder in ("inbox", "junkemail"):
                try:
                    r = requests.get("https://graph.microsoft.com/v1.0/me/mailFolders/%s/messages"
                                     "?$top=15&$select=subject,body,receivedDateTime,from&$orderby=receivedDateTime desc" % folder,
                                     headers={"Authorization": "Bearer " + at}, timeout=20)
                    for m in r.json().get("value", []):
                        recv = _adobe_code_epoch(m.get("receivedDateTime", ""))
                        if recv < fresh:
                            continue
                        subj = m.get("subject") or ""
                        body = _re.sub(r"<[^>]+>", " ", (m.get("body") or {}).get("content", "") or "")
                        frm = (((m.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()
                        if not ("adobe" in frm or "verif" in (subj + body).lower() or "code" in subj.lower()):
                            continue
                        mm = (_re.search(r"(?:code|验证码)\D{0,15}(\d{6})", body, _re.I)
                              or _re.search(r"(?<!\d)(\d{6})(?!\d)", body))
                        if mm:
                            cand.append((recv, mm.group(1)))
                except Exception:
                    pass
        if cand:
            cand.sort()
            return cand[-1][1]
        time.sleep(4)
    return None


def protocol_login_direct(console, proxy=None, log=print):
    """★母号【接码登录】(passwordRecovery credential=code)→ 返回 jil access_token;失败返回 ""。
    零密码、零浏览器:只用 admin_email + admin_refresh_token(Graph读码)。彻底绕过密码——密码错/被Adobe软锁/
    微软联合登录号没密码 全不影响。逆向自 Cookie登录导出工具/_adobe_direct.py,选企业ORG_ADMIN profile拿JIL token。
    实测 charlesbehwffrias 母号端到端通过 bps-il list_organizations。"""
    email = (console.get("admin_email") or "").strip()
    rt = console.get("admin_refresh_token") or ""
    cid = console.get("admin_client_id") or "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
    if not (email and rt):
        log("[接码登录] 缺 admin_email/admin_refresh_token")
        return ""
    CID = "ONESIE1"
    RU = "https://adminconsole.adobe.com/"
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
        ra = _req("POST", "/signin/v2/users/accounts?jslVersion=" + JSL,
                  data=json.dumps({"username": email, "usernameType": "EMAIL"}))
        if ra.status_code != 200:
            log("[接码登录] 查号重试 %d" % ra.status_code)
            return ""
        b2 = {"extraPbaChecks": False, "pbaPolicy": None, "username": email, "usernameType": "EMAIL",
              "accountType": "individual", "deviceInfo": {"lsId": str(uuid.uuid4()), "hdId": None}}
        rs = _req("POST", "/signin/v2/authenticationstate?purpose=passwordRecovery&jslVersion=" + JSL, data=json.dumps(b2))
        if rs.status_code not in (200, 201):
            log("[接码登录] 建认证态 %d(429=限流换IP重试)" % rs.status_code)
            return ""
        t0 = time.time()
        rsend = _req("POST", "/signin/v3/challenges?purpose=passwordRecovery&factor=email&extendedAuthState=false&jslVersion=" + JSL, data="{}")
        if rsend.status_code != 200:
            log("[接码登录] 发码 %d(429=限流)" % rsend.status_code)
            return ""
        code = _read_adobe_code_graph(cid, rt, t0, log=log)
        if not code:
            log("[接码登录] Graph读不到Adobe验证码")
            return ""
        rtok = _req("POST", "/signin/v3/tokens?credential=code&jslVersion=" + JSL,
                    data=json.dumps({"purpose": "passwordRecovery", "code": code}))
        susi = ""
        try:
            susi = rtok.json().get("token") or ""
        except Exception:
            pass
        if not susi:
            log("[接码登录] 验码失败 %d" % rtok.status_code)
            return ""
        log("[接码登录] ✅ 接码拿到SUSI(零密码),选企业profile…")
        # 企业后端:选 ORG_ADMIN profile → ims/tokens → fromSusi → access_token(=jil_token)
        s.headers["authorization"] = "Bearer " + susi
        rpf = s.get(B + "/signin/v2/accounts/filtered_profiles?filter=" + quote(PROFILE_FILTER), timeout=20)
        profs = []
        try:
            profs = rpf.json().get("filteredProfiles", [])
        except Exception:
            pass
        if not profs:
            log("[接码登录] 没企业profile %d(号非org-admin或限流)" % rpf.status_code)
            return ""
        ent = profs[0]
        guid = ent.get("userId")
        link = ent.get("linkId")
        s.put(B + "/signin/v1/filterprofilemapping", data=json.dumps({"filter": PROFILE_FILTER, "guid": guid}), timeout=20)
        if link:
            s.post(B + "/signin/v1/accounts/tokens", data=json.dumps({"linkId": link}), timeout=20)
        r3 = s.post(B + "/signin/v1/ims/tokens", data=json.dumps({"rememberMe": True, "reauthenticate": None}), timeout=20)
        mid = ""
        try:
            mid = r3.json().get("token") or ""
        except Exception:
            pass
        if not mid:
            log("[接码登录] ims/tokens %d 没mid" % r3.status_code)
            return ""
        callback = "https://ims-na1.adobelogin.com/ims/adobeid/%s/AdobeID/token?redirect_uri=%s" % (CID, quote(RU, safe=""))
        form = {"remember_me": "true", "callback": callback, "client_id": CID, "scope": ADMIN_SCOPE, "locale": "en_US",
                "state": '{"jslibver":"%s","nonce":"9999"}' % JSL, "flow_type": "token", "idp_flow_type": "login",
                "response_type": "token", "redirect_uri": RU, "use_ms_for_expiry": "true", "flow": "signIn", "token": mid}
        for k in ("authorization", STATE_HDR, IDV_HDR, "content-type", "x-ims-clientid"):
            s.headers.pop(k, None)
        r = s.post("https://adobeid-na1.services.adobe.com/ims/fromSusi", data=form, allow_redirects=True, timeout=30)
        import re as _re
        access_token = ""
        for u in [r.url] + [h.headers.get("location", "") for h in r.history] + [r.text or ""]:
            mm = _re.search(r"access_token=([^&#\"'\s]+)", u or "")
            if mm:
                from urllib.parse import unquote as _uq
                access_token = _uq(mm.group(1))
                break
        if not access_token:
            log("[接码登录] fromSusi没拿到access_token")
            return ""
        log("[接码登录] ✅✅ 零密码拿到 jil_token(%d字)" % len(access_token))
        # 存 session cookie 供 refresh_jil_via_cookie 静默续
        try:
            console["admin_session_cookie"] = json.dumps(
                [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path or "/"} for c in s.cookies],
                ensure_ascii=False)
        except Exception:
            pass
        return access_token
    except Exception as exc:
        log("[接码登录] 异常 %s" % str(exc)[:100])
        return ""


def protocol_login(console, proxy=None, log=print):
    """协议登录一个母号 → 返回最终 access_token(jil_token);失败返回 ""。纯 HTTP,不开浏览器、不依赖 admin_profile。
    ★优先【接码登录】(零密码,绕过密码软锁/联合登录号);失败再退回密码版。需 admin_email + admin_refresh_token(接码),
    密码版另需 admin_password。"""
    email = (console.get("admin_email") or "").strip()
    pw = console.get("admin_password") or ""
    rtok = console.get("admin_refresh_token") or ""
    cid = console.get("admin_client_id") or "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
    org = console.get("org_id") or ""
    prod = console.get("product_id") or ""
    # ① 优先接码登录(零密码,最稳):只要有 email + refresh_token
    if email and rtok:
        t = protocol_login_direct(console, proxy, log=log)
        if t:
            return t
        log("[协议登录] 接码登录没成 → 退回密码版")
    if not (email and pw and rtok):
        log("[协议登录] 缺凭证(需 admin_email/password/refresh_token)")
        return ""
    try:
        al = AdminLogin(email, pw, proxy=proxy)
        tok = al.run(rtok, cid, org, prod) or ""
        if tok:
            # ★存母号 session cookie(结构化 JSON,带域 name/value/domain/path):token 失效时走 authorize 静默续、免接码。
            #   不用平铺串——平铺把多域同名 cookie(如两个 ims_sid)一锅塞给 authorize 会让它不认、踢回登录("一成一败"的根因)。
            try:
                console["admin_session_cookie"] = json.dumps(
                    [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path or "/"}
                     for c in al.s.cookies], ensure_ascii=False)
            except Exception:
                pass
        return tok
    except Exception as exc:
        log("[协议登录] 异常 %s" % str(exc)[:120])
        return ""


def refresh_jil_via_cookie(console, proxy=None, log=print):
    """用母号存的 session cookie 走 authorize/v1 静默续 jil_token(access_token),免接码、免浏览器。
    复用 SUSI 登录当场种下的 ims_sid/aux_sid/relay 等 .services.adobe.com session cookie。失败(cookie过期等)返回 ""。"""
    raw = (console.get("admin_session_cookie") or "").strip()
    if not raw:
        return ""
    cid = console.get("admin_jil_client_id") or "ONESIE1"
    redirect_uri = "https://adminconsole.adobe.com/"
    s = requests.Session()
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    s.headers.update({"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"})
    # 还原 cookie:新格式=JSON(带域)→塞进 cookiejar,requests 按域只发 adobeid-na1.services.adobe.com
    #   该收的那几个(跟登录当场 self.s.get(authorize) 一致),不再多域同名一锅塞;旧格式=平铺串→兜底走 Cookie 头。
    hdr_cookie = None
    if raw[:1] == "[":
        try:
            from requests.cookies import create_cookie
            for it in json.loads(raw):
                if it.get("name"):
                    s.cookies.set_cookie(create_cookie(
                        name=it["name"], value=it.get("value") or "",
                        domain=it.get("domain") or "", path=it.get("path") or "/"))
        except Exception as _e:
            log("[cookie续期] 还原cookiejar失败,退回平铺: %s" % str(_e)[:60])
            hdr_cookie = raw
    else:
        hdr_cookie = raw
    au = ("https://adobeid-na1.services.adobe.com/ims/authorize/v1?client_id=" + cid +
          "&scope=" + quote(ADMIN_SCOPE) + "&response_type=token&locale=en_US&jslVersion=" + JSL +
          "&redirect_uri=" + quote(redirect_uri) +
          "&state=" + quote('{"jslibver":"%s","nonce":"9999"}' % JSL))
    import re as _re
    from urllib.parse import unquote as _uq
    try:
        _kw = {"allow_redirects": False, "timeout": 20}
        if hdr_cookie:
            _kw["headers"] = {"Cookie": hdr_cookie}
        ra = s.get(au, **_kw)
        loc = ra.headers.get("location", "")
        m = _re.search(r"access_token=([^&#]+)", loc)
        if m:
            log("[cookie续期] ✅ authorize 出 token(免接码)")
            return _uq(m.group(1))
        log("[cookie续期] authorize 没出 token(cookie 可能过期): %s loc=%s" % (ra.status_code, loc[:110]))
    except Exception as e:
        log("[cookie续期] ERR %s" % str(e)[:90])
    return ""


def _outlook_code_fn(refresh_token, client_id):
    def fn(t0):
        import firefly_register_yescaptcha as fr
        code, _link = fr._wait_for_outlook_adobe_email(refresh_token, client_id, timeout=120, fresh_after_ts=t0 - 10)
        return code
    return fn


def _cfworker_code_fn(email):
    def fn(t0):
        import firefly_register_yescaptcha as fr
        import requests as _rq
        import re as _re2
        import time as _t
        s = _rq.Session()
        fresh = t0 - 10   # ★按【发码时间】过滤,不用 old 快照——adpuhao worker 转发快、码邮件在拍快照那刻已到会被误判成旧(竞态→永远等不到)
        start = _t.time()
        while _t.time() - start < 120:
            cand = []  # 收发码之后的候选码,取时间最新的一封
            for it in fr._fetch_cfworker_emails(s, email):
                recv = fr._item_received_epoch(it)
                if recv is not None and recv < fresh:   # 比发码还旧 → 跳过(防抓到历史旧码)
                    continue
                text = fr._mail_item_text(it)
                if not _re2.search(r"adobe|firefly|verif|code", text, _re2.I):
                    continue
                code, link = fr._extract_adobe_code_or_link(text)
                if not code:
                    d = fr._fetch_cfworker_email_detail(s, it)
                    code, link = fr._extract_adobe_code_or_link(fr._mail_item_text(d))
                if code:
                    cand.append((recv or 0, code))
            if cand:
                # ★有时间戳的取最新那封;都没时间戳时才兜底用最后一封(不删候选,不让收码更脆弱)
                timed = [c for c in cand if c[0] > 0]
                return (max(timed, key=lambda x: x[0]) if timed else cand[-1])[1]
            _t.sleep(4)
        return None
    return fn


_SUB_COOKIE_WANT = ("ims_sid", "aux_sid", "relay", "fg", "gds", "filter-profile-map",
                    "filter-profile-map-permanent_prod", "filter-profile-map-permanent", "ftrset", "idg_token", "arid", "locale")


def sub_login_cookie_direct(account, proxy=None, log=print):
    """★子号【接码登录】(passwordRecovery,零密码)→ Firefly企业profile → 导出登录态 cookie。失败返回 ""。
    只用 email + refresh_token(Graph读码),绕过密码(密码错/软锁/联合登录号都不影响)。选企业profile(linkId非personal)拿4000那套。
    实测 particiaqvt-fanita 零密码出 2296字含ims_sid、企业4000。"""
    email = (account.get("email") or "").strip()
    rt = account.get("refresh_token") or ""
    cid = account.get("client_id") or "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
    if not (email and rt):
        return ""
    CID = "clio-playground-web"
    RU = "https://firefly.adobe.com/"
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
        ra = _req("POST", "/signin/v2/users/accounts?jslVersion=" + JSL,
                  data=json.dumps({"username": email, "usernameType": "EMAIL"}))
        if ra.status_code != 200:
            log("[子号接码] 查号重试 %d" % ra.status_code)
            return ""
        b2 = {"extraPbaChecks": False, "pbaPolicy": None, "username": email, "usernameType": "EMAIL",
              "accountType": "individual", "deviceInfo": {"lsId": str(uuid.uuid4()), "hdId": None}}
        rs = _req("POST", "/signin/v2/authenticationstate?purpose=passwordRecovery&jslVersion=" + JSL, data=json.dumps(b2))
        if rs.status_code not in (200, 201):
            log("[子号接码] 建认证态 %d(429=换IP重试)" % rs.status_code)
            return ""
        t0 = time.time()
        rsend = _req("POST", "/signin/v3/challenges?purpose=passwordRecovery&factor=email&extendedAuthState=false&jslVersion=" + JSL, data="{}")
        if rsend.status_code != 200:
            log("[子号接码] 发码 %d" % rsend.status_code)
            return ""
        code = _read_adobe_code_graph(cid, rt, t0, log=log)
        if not code:
            log("[子号接码] Graph读不到码")
            return ""
        rtok = _req("POST", "/signin/v3/tokens?credential=code&jslVersion=" + JSL,
                    data=json.dumps({"purpose": "passwordRecovery", "code": code}))
        susi = ""
        try:
            susi = rtok.json().get("token") or ""
        except Exception:
            pass
        if not susi:
            log("[子号接码] 验码失败 %d" % rtok.status_code)
            return ""
        # Firefly企业后端:宽松filter选【企业profile(有linkId非personal)→4000】
        s.headers["authorization"] = "Bearer " + susi
        pf = '{"fallbackToAA":true}'
        rpf = s.get(B + "/signin/v2/accounts/filtered_profiles?filter=" + quote(pf), timeout=20)
        profs = []
        try:
            profs = rpf.json().get("filteredProfiles", [])
        except Exception:
            pass
        ent = (next((p for p in profs if p.get("linkId") and "personal" not in (p.get("description") or "").lower()), None)
               or next((p for p in profs if p.get("linkId")), None))
        if not ent:
            # ★选不到企业profile(权益没传播/限流)→ 绝不退选personal(普号10废cookie),返回空让上层换IP/等传播重试
            log("[子号接码] 没企业profile %d(权益没传播/限流,不退personal)" % rpf.status_code)
            return ""
        s.put(B + "/signin/v1/filterprofilemapping", data=json.dumps({"filter": pf, "guid": ent.get("userId")}), timeout=20)
        if ent.get("linkId"):
            _rat = s.post(B + "/signin/v1/accounts/tokens", data=json.dumps({"linkId": ent.get("linkId")}), timeout=20)
            # ★2026-07-16 修 ims/tokens 401 invalid_token:accounts/tokens 返回的才是【企业 profile 上下文】token,
            #   susi(接码)token 绑个人 AdobeID,ims/tokens 用它→Invalid JWT→401→没mid→空cookie误判普号。换成企业token。
            try:
                _ent_tok = _rat.json().get("token") or ""
            except Exception:
                _ent_tok = ""
            if _ent_tok:
                s.headers["authorization"] = "Bearer " + _ent_tok
        r3 = s.post(B + "/signin/v1/ims/tokens", data=json.dumps({"rememberMe": True, "reauthenticate": None}), timeout=20)
        mid = ""
        try:
            mid = r3.json().get("token") or ""
        except Exception:
            pass
        if not mid:
            log("[子号接码] ims/tokens %d 没mid" % r3.status_code)
            return ""
        callback = "https://ims-na1.adobelogin.com/ims/adobeid/%s/AdobeID/token?redirect_uri=%s" % (CID, quote(RU, safe=""))
        form = {"remember_me": "true", "callback": callback, "client_id": CID, "scope": FIREFLY_SCOPE, "locale": "en_US",
                "state": '{"jslibver":"%s","nonce":"9999"}' % JSL, "flow_type": "token", "idp_flow_type": "login",
                "response_type": "token", "redirect_uri": RU, "use_ms_for_expiry": "true", "flow": "signIn", "token": mid}
        for k in ("authorization", STATE_HDR, IDV_HDR, "content-type", "x-ims-clientid"):
            s.headers.pop(k, None)
        s.post("https://adobeid-na1.services.adobe.com/ims/fromSusi", data=form, allow_redirects=True, timeout=30)
        got = {c.name: c.value for c in s.cookies if c.name in _SUB_COOKIE_WANT}
        ck = "; ".join("%s=%s" % (k, v) for k, v in got.items())
        if "ims_sid" not in got:
            log("[子号接码] fromSusi没种到ims_sid")
            return ""
        log("[子号接码] ✅ 零密码拿到企业cookie %d字" % len(ck))
        return ck
    except Exception as exc:
        log("[子号接码] 异常 %s" % str(exc)[:100])
        return ""


def sub_login_cookie(account, proxy=None, log=print):
    """子号协议登录 Firefly → 导出登录态 cookie(给 adobe2api cookie-replay 查积分)。失败返回 ""。
    account: {email, password, [refresh_token, client_id]} —— 有 refresh_token 走 outlook 拿码,否则 cloudflare worker。
    ★优先【接码登录】(零密码,有RT时):绕过密码错/软锁/联合登录号;失败再退回密码版。
    ★用 Firefly 上下文(clio-playground-web)登录,filtered_profiles 才返回企业 profile(团队号),拿到 4000 那套登录态。"""
    email = (account.get("email") or "").strip()
    pw = account.get("password") or ""
    rt = account.get("refresh_token") or ""
    # ① 优先接码登录(零密码,最稳):只要有 email + refresh_token
    if email and rt:
        ck = sub_login_cookie_direct(account, proxy, log=log)
        if ck:
            return ck
        log("[子号协议] 接码没成 → 退回密码版")
    if not (email and pw):
        log("[子号协议] 缺 email/password(且接码不可用)")
        return ""
    mail_cid = account.get("client_id") or "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
    mail_fn = _outlook_code_fn(rt, mail_cid) if rt else _cfworker_code_fn(email)
    try:
        a = AdminLogin(email, pw, proxy=proxy, client_id="clio-playground-web",
                       scope=FIREFLY_SCOPE, redirect_uri="https://firefly.adobe.com/")
        return a.run("", "", "", "", mail_code_fn=mail_fn, want="cookie", profile_filter='{"preferForwardProfile": true};') or ""
    except Exception as exc:
        log("[子号协议] 异常 %s" % str(exc)[:120])
        return ""


if __name__ == "__main__":
    import sys as _sys
    idx = int(_sys.argv[1]) if len(_sys.argv) > 1 else 0
    cfg = json.load(open("admin_console_config.json", encoding="utf-8-sig"))
    c = cfg["consoles"][idx]
    print("母号[%d]:" % idx, c["admin_email"], "org:", c.get("org_id"), flush=True)
    t = protocol_login(c)
    print("\n最终 jil_token: len=%d head=%s" % (len(t), t[:24]), flush=True)
