# 完整全流程协议:从母号到 adobe 的导入(纯 HTTP、零浏览器)

> 2026-06-17 端到端实测全通。母号登录→列子号→子号导出cookie→推adobe2api,全程纯 requests,不开浏览器、不依赖 admin_profile/ms-playwright。

## 一键跑(命令行)
```
python full_flow_protocol.py <母号email或索引> [--limit N] [--workers N] [--check-credits] [--push]
```
- `--limit N`: 只导前 N 个子号(实测/试跑用,默认 2)
- `--check-credits`: 导出后查 Firefly 积分(验证真团队号=4000)
- `--push`: 推 adobe2api(生产 103.195.102.125)
- 不带 --limit 改大/去掉即可全量

**实测(2026-06-17)**:
```
① 母号 bontrager... 协议登录 → jil_token 1881字
② JIL 真团队子号 9个 → ③ 账号库匹配密码
④ 子号 ff47xd2... 协议导出 → cookie 2576字 | 积分 1480/4000
⑤ 推 adobe2api → accepted, 入池 1/1
```
另一母号导2个子号: ff3gmj... 3600/4000、ff3j1jj... 2920/4000,并发2跑通。

## 流程五步(full_flow_protocol.py)
1. **母号协议登录** `admin_login_protocol.protocol_login(console)` → jil_token(详见 _PROTOCOL_admin_login.md)
2. **列真团队子号** `adobe_jil.list_product_users(org, product, token)` → 子号 email(★本地 console_children 和 Adobe 实际成员可能不同步,以 JIL 为准)
3. **账号库匹配密码** added_accounts.txt / registered_accounts.txt(email----密码----[邮箱密码]----[client_id]----[refresh_token]),建 email→{password,refresh_token,client_id} 索引
4. **子号协议登录导出 cookie** `admin_login_protocol.sub_login_cookie(account)`:
   - ★**必须用 Firefly 上下文**:`client_id=clio-playground-web` + `FIREFLY_SCOPE` + `redirect_uri=firefly.adobe.com`(母号是 ONESIE1/adminconsole)
   - 拿码自动选:有 outlook refresh_token 走 outlook,否则 cloudflare worker(adpuhao 邮箱,mailapi.adpuhao.xyz)
   - ★**子号在 Firefly 上下文 filtered_profiles 返回 Personal + 企业profile(团队号)**,选企业 profile(filterprofilemapping)→ fromSusi 设 cookie → 这就是 4000 那套登录态
   - 用 ONESIE1/adminconsole 登录子号会只返 personal → 查积分=10(普号假成功),所以子号一定要 Firefly 上下文
   - 导出 self.s.cookies(含 ims_sid/aux_sid/filter-profile-map,~2500字)
5. **推 adobe2api** `cookie_push._push_now(母号, [{email,cookie}], force=True)`(现成,生产一直用的)

## 关键认知(踩坑记录)
- **10 积分 = 普号个人额度;4000 = 团队真额度**。子号必须在 Firefly 上下文选到企业 profile 才拿 4000。
- **母号 vs 子号上下文不同**:母号 adminconsole(ONESIE1,有企业admin profile);子号 Firefly(clio-playground-web,有企业member profile)。
- **拿码**:母号 hotmail 走 outlook RT;子号 adpuhao 走 cloudflare worker。个别 adpuhao 邮箱 cloudflare 转发慢/丢,拿不到码就跳过换下一个。
- **MFA 发码限流**:同号短时反复发码 `confirmation_abuse_detected`;同 IP 太多登录会被风控 → 建议分散 IP(节点池/代理)+ token/cookie 缓存复用别频繁重登。
- **无 Arkose**:母号 adminconsole、子号 Firefly 登录全程没出 Arkose。

## 性能(vs 浏览器版)
- 单号快 2-3 倍(省浏览器启动/渲染/反爬重试);并发 10-30 倍(每号 KB 级 vs chromium 几百MB);整体吞吐 15-50 倍。瓶颈只剩"等邮箱码 + Adobe 风控容忍度"。

## 下一步(待和用户确认)
- 接入团队工具:让【导出子号CK】/换号的 --then-extract 路由**协议优先、浏览器兜底**(改 app.py 导出路由 + firefly_login_extract,或团队工具直接调 full_flow_protocol.py)。
- 关联 [[firefly-protocol-version]]、_PROTOCOL_admin_login.md。
