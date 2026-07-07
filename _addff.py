# -*- coding: utf-8 -*-
"""把给的 14 个 @adpuhao.xyz FF号 加进有空位的新母号当子号 → sub_login_cookie(密码+cf worker读码)
   → 查积分(4000=有效企业子号)+ probe408(本地预期408=环境问题)。"""
import os, sys, time
import concurrent.futures as cf
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import admin_console_manage as acm
import admin_jil_refresh_token as rtok
import adobe_jil as jil
import admin_login_protocol as alp
import network_proxy
import _quota
import _probe408 as P408

RAW = """f01gzjyv79s4@adpuhao.xyz----PENGpeng5211314!
ff0246xcaz1qinez6@adpuhao.xyz----PENGpeng5211314!
ff051gfun1b1yw1lqb@adpuhao.xyz----PENGpeng5211314!
ff05rg95u4wsj97zq@adpuhao.xyz----PENGpeng5211314!
ff0ap5bcmnoih@adpuhao.xyz----PENGpeng5211314!
ff0bdrf1migg7y@adpuhao.xyz----PENGpeng5211314!
ff0dvnq1v6ews5zr@adpuhao.xyz----PENGpeng5211314!
ff0hhn1nwadyc0@adpuhao.xyz----PENGpeng5211314!
ff0jgqzlos73j39c@adpuhao.xyz----PENGpeng5211314!
ff0kyynm3c880t@adpuhao.xyz----PENGpeng5211314!
ff0l36ci0yg4w01n@adpuhao.xyz----PENGpeng5211314!
ff0mqzvk0om4d@adpuhao.xyz----PENGpeng5211314!
ff0r90vzc4dv8zi@adpuhao.xyz----PENGpeng5211314!
ff0rb54cdcldic3rpl@adpuhao.xyz----PENGpeng5211314!"""
accts = []
for l in RAW.splitlines():
    p = l.strip().split("----")
    if len(p) >= 2 and "@" in p[0]:
        accts.append({"email": p[0].strip(), "password": p[1].strip()})
print("待加 %d 个 @adpuhao 号" % len(accts), flush=True)

cfg, cons = acm._load_consoles()
NEW = ["hammettforthman9243", "lannigandomin684", "gurevichkaauamo05", "picknellpies1009", "carstensray216"]
masters = [c for c in cons if any(n in (c.get("admin_email") or "").lower() for n in NEW) and c.get("org_id")]

# 分配到有空位的母号
added = []   # (acct, console)
ai = 0
for c in masters:
    if ai >= len(accts):
        break
    sel = c.get("admin_email"); org = c.get("org_id"); prod = c.get("product_id")
    px = network_proxy.proxy_for_console(c)
    tok, _did = rtok.ensure_token(c, px)
    if not tok:
        print("[%s] 母号登录失败,跳过" % sel, flush=True); continue
    jil.set_console_proxy(px)
    users = jil.list_product_users(org, prod, tok)
    free = max(0, 10 - len(users))
    if free <= 0:
        print("[%s] 满了(0空位)" % sel, flush=True); continue
    groups = jil.get_license_groups(org, prod, tok)
    lg = c.get("license_group_id") or (groups[0]["id"] if groups else None)
    batch = accts[ai:ai + free]
    ems = [a["email"] for a in batch]
    try:
        res = jil.add_users(org, prod, lg, tok, ems)
    except Exception as e:
        jil.set_console_proxy(None)
        res = jil.add_users(org, prod, lg, tok, ems)
        jil.set_console_proxy(px)
    okset = set()
    for chunk in (res or []):
        for it in (chunk.get("body") or []):
            rc = it.get("responseCode"); em = ((it.get("request") or {}).get("email") or "").lower()
            ec = (it.get("response") or {}).get("errorCode", "")
            print("  [%s] +%s rc=%s %s" % (sel.split("@")[0], em[:24], rc, ec), flush=True)
            if rc in (200, 201):
                okset.add(em)
    for a in batch:
        if a["email"].lower() in okset:
            added.append((a, c))
    ai += len(batch)

print("\n加成功 %d 个子号" % len(added), flush=True)
if not added:
    print("没加成功,停"); sys.exit(0)
print("#### 等权益传播 120s ####", flush=True)
time.sleep(120)


def test_one(item):
    a, c = item
    em = a["email"]; px = network_proxy.proxy_for_id(em); pr = {"http": px, "https": px}
    try:
        ck = alp.sub_login_cookie({"email": em, "password": a["password"]}, proxy=px)  # 无RT→cf worker读码
    except Exception as e:
        return (em, "登录异常:%s" % str(e)[:24], None)
    if not ck:
        return (em, "cookie失败(cf读码?)", None)
    tok, _ = _quota._refresh_to_token(ck, pr)
    cred = (_quota._fetch_credits(tok, pr) or {}).get("total") if tok else None
    st, _ok = P408.probe_one(ck, px)
    return (em, st, cred)


print("\n=== 逐子号 登录+积分+probe408(并发4)===", flush=True)
with cf.ThreadPoolExecutor(max_workers=4) as ex:
    for em, st, cred in ex.map(test_one, added):
        print("  %-34s 积分=%s | 408探测=%s" % (em[:34], cred, st), flush=True)
