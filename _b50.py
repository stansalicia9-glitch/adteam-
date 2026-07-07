# -*- coding: utf-8 -*-
"""从50.txt取10个号→加到一个活母号→等传播→逐个登录查积分+探408。看别处来源的号能不能出图。"""
import sys, os, time, hashlib, base64, random, re, json
import importlib.util as _ilu
import admin_console_manage as acm, console_children, network_proxy
import admin_jil_refresh_token as rt
import adobe_jil as jil
import admin_login_protocol as alp
import _quota
from curl_cffi import requests as creq
_spec = _ilu.spec_from_file_location("a2a_payloads", r"E:\adobe2api-master\core\models\payloads.py")
_pmod = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_pmod)
bipc = _pmod.build_image_payload_candidates
SUBMIT = "https://firefly-3p.ff.adobe.io/v2/3p-images/generate-async"; APIK = "clio-playground-web"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
FILE = r"D:\QQ聊天\920629427\FileRecv\50(1).txt"


def _uid(t):
    try:
        p = t.split(".")[1]; p += "=" * (-len(p) % 4)
        return str(json.loads(base64.urlsafe_b64decode(p)).get("user_id") or "")
    except Exception:
        return ""


def q408(ck, px):
    pr = {"http": px, "https": px} if px else None
    tok, _ = _quota._refresh_to_token(ck, pr)
    if not tok:
        return None, "ckdead"
    q = _quota._fetch_credits(tok, pr) or {}; tot = q.get("total")
    pls = bipc(prompt="a small red apple", aspect_ratio="1:1", output_resolution="1K",
              upstream_model_id="gemini-flash", upstream_model_version="nano-banana-2")
    uid = _uid(tok); nonce = hashlib.sha256(("%s-a small red apple" % uid).encode()).hexdigest() if uid else ""
    h = {"Authorization": "Bearer " + tok, "x-api-key": APIK, "content-type": "application/json", "accept": "*/*",
         "user-agent": UA, "origin": "https://firefly.adobe.com", "referer": "https://firefly.adobe.com/"}
    if nonce:
        h["x-nonce"] = nonce
    try:
        r = creq.post(SUBMIT, headers=h, json=pls[0], impersonate="chrome124", timeout=60, proxies=pr, verify=False)
        return tot, r.status_code
    except Exception as e:
        return tot, "err:%s" % str(e)[:20]


accts = []
for line in open(FILE, encoding="utf-8-sig"):
    segs = re.split(r"----|\s+", line.strip())
    if len(segs) >= 2 and "@" in segs[0]:
        rt_ = next((s for s in segs[2:] if s.startswith("M.")), "")
        cid = next((s for s in segs[2:] if len(s) == 36 and s.count("-") == 4), "")
        accts.append({"email": segs[0].strip(), "password": segs[1].strip(), "refresh_token": rt_, "client_id": cid})
accts = accts[:10]
print("从 50.txt 取了 %d 个号" % len(accts), flush=True)

cfg, cons = acm._load_consoles()
target = None
for c in cons:
    if "gathonipetakas" in ((c.get("name") or "") + (c.get("admin_email") or "")).lower():
        continue
    sel = c.get("admin_email") or c.get("name") or ""
    if (console_children.get_children(sel) and c.get("admin_password") and c.get("admin_refresh_token")
            and c.get("org_id") and c.get("product_id")):
        target = c; break
sel = target.get("admin_email") or target.get("name")
proxy = network_proxy.proxy_for_console(target)
print("=== 加到活母号: %s ===" % sel, flush=True)
tok, did = rt.ensure_token(target, proxy)
if not tok:
    print("❌ 母号登录失败"); sys.exit(1)
org = target.get("org_id") or jil.org_id_from_url(target.get("product_users_url", ""))
prod = target.get("product_id") or jil.product_id_from_url(target.get("product_users_url", ""))
groups = jil.get_license_groups(org, prod, tok)
lg = target.get("license_group_id") or (groups[0]["id"] if groups else None)
users = jil.list_product_users(org, prod, tok)
keep = {e.lower() for e in (target.get("keep_admin_emails") or [target.get("admin_email")]) if e}
to_rem = [u for u in users if u["email"].lower() not in keep and u.get("id")]
if to_rem:
    jil.remove_users(org, prod, lg, tok, [u["id"] for u in to_rem])
    print("删旧子号 %d 个,停15s" % len(to_rem), flush=True)
    time.sleep(random.uniform(13, 17))
res = jil.add_users(org, prod, lg, tok, [a["email"] for a in accts])
if did:
    acm.save_consoles_merge([target])
ok_emails = []
for chunk in (res or []):
    for it in (chunk.get("body") or []):
        if it.get("responseCode") in (200, 201):
            ok_emails.append(((it.get("request") or {}).get("email") or "").lower())
print("JIL 加成功 %d / %d 个" % (len(ok_emails), len(accts)), flush=True)
print("#### 等 180s 权益传播... ####", flush=True)
time.sleep(180)

print("\n=== 逐个登录 + 查积分 + 探408 ===", flush=True)
c200 = c408 = bad = 0
for a in accts:
    em = a["email"]
    pxy = network_proxy.proxy_for_id(em)
    ck = alp.sub_login_cookie(a, proxy=pxy)
    if not ck:
        print("  %-38s 登录失败/导cookie失败" % em[:38], flush=True); bad += 1; continue
    tot, st = q408(ck, pxy)
    tag = "✅能用200" if st == 200 else ("❌408" if st == 408 else "⚠%s" % st)
    if st == 200:
        c200 += 1
    elif st == 408:
        c408 += 1
    else:
        bad += 1
    print("  %-38s 积分%s | %s" % (em[:38], tot, tag), flush=True)
    time.sleep(random.uniform(1.2, 2))
print("\n==== 结论:能出图200 %d | 408 %d | 登录/其它失败 %d ====" % (c200, c408, bad), flush=True)
print(">>> 有200 = 这批号能用、是你之前那批被标; 全408 = 这批也被掐(同类型/同源问题或全局)", flush=True)
