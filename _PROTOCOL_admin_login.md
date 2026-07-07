# 母号 Adobe IMS 协议登录 —— 抓包逆向蓝图

> 2026-06-16 用 MCP playwright 实登录 `metzgerxilftrteel@hotmail.com` 抓的完整链路。
> 目标:用 Python `requests` 复刻,替代 admin_seed_login.py 的浏览器登录,拿到 `jil_token`+`org_id`。
> **实测:全程无 Arkose**(邮箱→邮箱码→密码→选企业);拿到的 token 调 JIL 列子号 200/8个,闭环已验证。

## 一、整体流程(浏览器实测路由)
`adminconsole.adobe.com` → 跳 `auth.services.adobe.com` 登录页 →
① 输邮箱 → ② `challenge/verify/email`(点 Continue 发码)→ ③ `challenge/mfa/code`(填邮箱码)→
④ `password`(填密码)→ ⑤ `add-secondary-email`(Not now 跳过)→ ⑥ `profile-chooser`(选企业 profile)→
落地 `adminconsole.adobe.com/<org_id>/overview`,`window.adobeIMS.getAccessToken().token` = jil_token。

## 二、★state 链机制(协议核心)
所有 `auth.services.adobe.com/signin/*` 请求靠**两个加密 token 在请求头链式传递状态**(不是 cookie):
- `x-ims-authentication-state-encrypted` : 加密认证状态,**每个响应的 response header 回吐新值,下一请求的 request header 带上**
- `x-identity-verification-token` : 身份验证 JWE(RSA-OAEP-256),同样 response header → next request header
- 响应头 `access-control-expose-headers: ... X-IMS-Authentication-State-Encrypted, X-Identity-Verification-Token` 把它们暴露给前端读

固定请求头:`x-ims-clientid: ONESIE1`、`content-type: application/json`、`accept: application/json, text/plain, */*`、正常 UA。
配 `requests.Session()`(cookie 也维护着,access-control-allow-credentials: true)。

## 三、完整 API 链(method / url / body / 说明)
基址 `B = https://auth.services.adobe.com`,所有带 `?jslVersion=v2-v0.31.0-2-g1e8a8a8`(可带可不带)。

1. `GET  B/signin/v2/configurations/ONESIE1` → 登录配置(初始化,可能回首个 state)
2. `POST B/signin/v2/users/accounts`  body `{"username":"<email>","usernameType":"EMAIL"}` → 查号,**响应头回首个 x-ims-authentication-state-encrypted + x-identity-verification-token**
3. `POST B/signin/v2/authenticationstate?purpose=multiFactorAuthentication` → [201] 建认证状态(回新 state)
4. `GET  B/signin/v3/challenges?purpose=multiFactorAuthentication` → 列可用因子(含 email)
5. `POST B/signin/v3/challenges?purpose=multiFactorAuthentication&factor=email&extendedAuthState=false`  body `{}` → **触发发邮箱验证码**
6. 【拿码】`firefly_register_yescaptcha._wait_for_outlook_adobe_email(refresh_token, client_id)` → 6位码(已验证可用,refresh_token→Graph 读邮件)
7. `PUT  B/signin/v3/challenges?purpose=multiFactorAuthentication`  body `{"code":"<6位>"}` → **验邮箱码**
8. `POST B/signin/v2/tokens?credential=password`  body `{"username":"<email>","usernameType":"EMAIL","password":"<pw>","accountType":"individual","rememberMe":true}` → **验密码**(明文 body,靠 HTTPS)
9. `GET  B/signin/v2/accounts/filtered_profiles?filter=...hasRole('ORG_ADMIN')+or+hasRole('PRODUCT_ADMIN')+...` → 列管理员 profile(对应 org)
10. (可选)`PUT B/signin/v1/accounts/<userGUID>/profileData/actions/SecondaryEmail` → 跳过备用邮箱(浏览器是 Not now,协议可不调)
11. `PUT  B/signin/v1/filterprofilemapping` → 选定 profile(企业)
12. `POST B/signin/v1/accounts/tokens` → 账号 token
13. `POST B/signin/v1/ims/tokens` → [200] **拿最终 IMS token = jil_token**(response body,待 Python 实跑确认字段)

旁路(可忽略):`POST /signin/v2/tokens?credential=sso` → 401(无现成会话,正常);`GET /signin/v1/captcha/encryptedData`(隐形 captcha,本次没拦)。

## 四、已确认的 request body(抓包原文)
- 查号(189): `{"username":"metzgerxilftrteel@hotmail.com","usernameType":"EMAIL"}`
- 发码(215): `{}`(factor 在 query)
- 验码(221): `{"code":"778754"}`
- 验密码(232): `{"username":"...","usernameType":"EMAIL","password":"...","accountType":"individual","rememberMe":true}`

## 五、闭环验证(已通)
拿到 token 后,纯 HTTP 调 JIL(adobe_jil.py 已现成):
`GET https://bps-il.adobe.io/jil-api/v2/organizations/<org_id>/products/<product_id>/users?page=0&page_size=N`
头 `Authorization: Bearer <jil_token>` + `X-Api-Key: ONESIE1` → 200,列出子号。实测 8 个子号正常返回。

## 六、admin_login_protocol.py 实跑进度(2026-06-16,纯 Python 已验证)
✅ **已跑通(状态链全对)**:
1. `GET /signin/v2/configurations/ONESIE1` 200
2. `POST /signin/v2/users/accounts` `{"username":email,"usernameType":"EMAIL"}` 200(账号 authenticationMethods=[otp,password])
3. `POST /signin/v2/authenticationstate?purpose=multiFactorAuthentication` **body 必须 `{"username":email,"usernameType":"EMAIL","accountType":"individual"}`**(★accountType 是缺的关键字段;只发 username 报 "type null") → 201 + 首个 state header(mfaStatus:REQUIRED, requireCaptcha:true 但不拦)
4. `GET /signin/v3/challenges?purpose=multiFactorAuthentication` 200 availableFactors=[email]
5. `POST /signin/v3/challenges?...&factor=email&extendedAuthState=false` body `{}` 200 → 发码
6. `_wait_for_outlook_adobe_email(rt,cid,fresh_after_ts=发码时刻)` 拿新码
7. `PUT /signin/v3/challenges?purpose=multiFactorAuthentication` `{"code":"xxxxxx"}` 200
8. `POST /signin/v2/tokens?credential=password` `{username,usernameType:EMAIL,password,accountType:individual,rememberMe:true}` → **200,响应体 `{"token":"<SUSI JWT>"}`**(credential_type:password)
9. **password 后清掉 state/idv 头,改用 `Authorization: Bearer <SUSI token>`**(cookie-only / idv 都 403,只有 Bearer 200)
10. `GET /signin/v2/accounts/filtered_profiles?filter=...` (Bearer) 200 → `filteredProfiles:[{userId,description,linkId}]`(本号 2 个:Personal + 企业"Kristen Mccall Investments" userId=DF6F...@df6e...e)
11. `POST /signin/v1/ims/tokens` (Bearer) body `{}` → 200 `{"token":<中间JWT>}`,该 token **aud=ims-na1.adobelogin.com**(不是 access_token,直接调 JIL 报 401 oauth invalid)

✅ **A. 选企业 profile —— 真实 body 抓到了(浏览器抓包确认,都 200)**:
- ★password 后 **state/idv 头要【保留】**(filterprofilemapping 请求头带着 `x-ims-authentication-state-encrypted`),只额外加 `Authorization: Bearer <SUSI>`(我之前清掉 state 头是错的)
- `PUT /signin/v1/filterprofilemapping` body=`{"filter":<同filtered_profiles的filter>, "guid":"<企业profile userId>"}`(★字段叫 `guid` 不是 selectedAccountGuid;值是 filtered_profiles 返回的企业项 userId,如 `DF6F...@df6e...e`)→ 200
- `POST /signin/v1/accounts/tokens` body=`{"linkId":"<企业profile linkId>"}`(11F1...)→ 200
- `POST /signin/v1/ims/tokens` body=`{"rememberMe":true,"reauthenticate":null}` → 200 中间 token

✅ **B. 中间 token → 最终 access_token —— 完全攻克(2026-06-16,纯协议闭环 JIL 200)**:
1. `POST https://adobeid-na1.services.adobe.com/ims/fromSusi`(**content-type: application/x-www-form-urlencoded**),body 字段:`token=<上一步 ims/tokens 的中间token>`★ + `callback=https://ims-na1.adobelogin.com/ims/adobeid/ONESIE1/AdobeID/token?redirect_uri=<urlencoded adminconsole>` + `client_id=ONESIE1` + `scope=<全套>` + `response_type=token` + `flow_type=token` + `flow=signIn` + `idp_flow_type=login` + `code_challenge_method=plain` + `redirect_uri=https://adminconsole.adobe.com/` + `state={"jslibver":...,"nonce":...}` + `remember_me=true` + `use_ms_for_expiry=true` + `locale=en_US`(relay/ecid 可省)。
2. fromSusi → **200** + 设 `ims_sid`/`aux_sid`/`filter-profile-map` cookie,**响应体是 `<html><head><meta http-equiv="refresh" content="0;url=https://adminconsole.adobe.com/#access_token=<最终jil_token>&token_type=bearer&expires_in=86400000">`** —— ★**access_token 直接在响应 HTML 的 meta-refresh URL 的 `#access_token` 里**(不是 302 Location、不是 form、是 meta-refresh,从 `r.text` 正则 `access_token=([^&#"'\s]+)` 提)。
3. 这个 access_token(~1881字, aud=ONESIE1, user_id=企业profile)就是 jil_token,`Authorization: Bearer <it>` + `X-Api-Key: ONESIE1` 调 bps-il JIL → 200 列子号。adobe2api 推送/换号要的就是它。

⚠️ **MFA 发码限流(实测踩到)**:短时间对同一母号反复发码 → `send-code 400 confirmation_abuse_detected "Too many factor confirmation attempts"`。开发调试要么换母号、要么间隔几十分钟;生产里每号登录一次、token 缓存复用(24h)不会触发。

**结论**:母号 Adobe 协议登录 **100% 纯 HTTP 跑通**(`admin_login_protocol.py` run() 返回 access_token,实测母号 index 11 "Harrell Design PLC" 全链通 → JIL 200 列子号)。零浏览器、零 Arkose,唯一外部依赖=邮箱验证码(已有协议拿码 `_wait_for_outlook_adobe_email`)。**下一步工程化**:① 把 run() 拿到的 access_token 存回 admin_console_config.json 的 `jil_token` ② 包装成团队工具可调函数(替代 admin_seed_login 浏览器登录)③ token 过期(24h)自动重登。关联 [[firefly-register-anticaptcha]]。
