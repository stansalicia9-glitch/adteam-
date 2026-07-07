# -*- coding: utf-8 -*-
"""深查子号为啥拿不到 4000:看母号 org 的 products / license-groups,确认换号 add_users 选的 lg 是不是 Firefly 那个。
用法: python _diag_entitlement.py [母号关键字]"""
import sys
import admin_console_manage as acm
import admin_login_protocol as alp
import adobe_jil as jil
import admin_jil_manage as ajm
import console_children

jil.PROXIES = None
cfg, cs = acm._load_consoles()
key = sys.argv[1] if len(sys.argv) > 1 else "byrne"
c = next((x for x in cs if key.lower() in (x.get("admin_email") or "").lower()), None)
if not c:
    print("没找到母号", key); sys.exit(1)
print("母号:", c["admin_email"], "| 配置product_id:", c["product_id"])
org, prod = c["org_id"], c["product_id"]
tok = c.get("jil_token") or ""
if tok:
    try:
        jil.list_products(org, tok)  # 测旧 token 是否还有效
        print("(用 config 已存 jil_token,免重登避免限流)")
    except Exception:
        tok = ""
if not tok:
    tok = alp.protocol_login(c)
if not tok:
    print("❌母号登录失败(token过期且重登失败)"); sys.exit(1)

print("\n--- org 所有 products ---")
prods = jil.list_products(org, tok)
for p in prods:
    mark = "  ←配置用的这个" if p.get("id") == prod else ""
    print("  id=%s name=%s total=%s%s" % (p.get("id"), p.get("name"), p.get("total"), mark))

print("\n--- 配置 product 的 license-groups ---")
groups = jil.get_license_groups(org, prod, tok)
for g in groups:
    print("  id=%s name=%s" % (g.get("id"), g.get("name")))
lg = ajm._pick_lg(groups, c)
print("换号 add_users 会选的 lg:", lg)

users = jil.list_product_users(org, prod, tok)
me = (c["admin_email"] or "").lower()
subs = [u for u in users if u["email"].lower() != me]
local = console_children.get_children(c) or []
print("\n配置 product 里子号 %d 个 | 本地清单 %d 个" % (len(subs), len(local)))
print("product 里前几个成员:", [(u["email"], u.get("id")) for u in users[:4]])
