# -*- coding: utf-8 -*-
"""坐实:子号【登录拿到的 userId】和它【在 org 里的成员 userId】是不是同一个账号身份。
不同 → TYPE2E 把子号建成了独立企业账号(@org.e),子号个人登录(@AdobeID)看不到企业4000。
用法: python _diag_member_match.py [母号关键字] [第几个子号]"""
import sys, re
import admin_console_manage as acm
import admin_login_protocol as alp
import adobe_jil as jil
import console_children

jil.PROXIES = None
cfg, cs = acm._load_consoles()
key = sys.argv[1] if len(sys.argv) > 1 else "morgan"
nth = int(sys.argv[2]) if len(sys.argv) > 2 else 0
c = next((x for x in cs if key.lower() in (x.get("admin_email") or "").lower()), None)
org, prod = c["org_id"], c["product_id"]
tok = c.get("jil_token") or ""
try:
    jil.list_products(org, tok)
except Exception:
    tok = alp.protocol_login(c)

users = jil.list_product_users(org, prod, tok)
org_uid = {u["email"].lower(): u.get("id") for u in users}

ch = console_children.get_children(c)[nth]
segs = re.split(r"----|\s+", str(ch.get("raw") or "").strip())
acc = {"email": segs[0], "password": segs[1] if len(segs) > 1 else "", "refresh_token": "", "client_id": ""}
print("@@@ 子号:", acc["email"], flush=True)
print("@@@ 它在 org 里的成员 userId:", org_uid.get(acc["email"].lower()), flush=True)
print("@@@ 下面 [profiles-all] 里的 userId = 它登录拿到的账号身份:", flush=True)
ck = alp.sub_login_cookie(acc, proxy=None)
print("@@@ 导出cookie字数:", len(ck) if ck else 0, flush=True)
