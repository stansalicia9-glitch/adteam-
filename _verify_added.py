# -*- coding: utf-8 -*-
"""验证 console_children 里的子号是否【真的加进了 Adobe 团队 org】:
母号协议登录 → JIL list_product_users(org实际成员) → 和本地子号清单对比。
全在=真加进去了(全10是权益传播延迟);有缺=没真加进去(那些永远10)。
用法: python _verify_added.py [母号关键字...](默认 clinebarclay ellisisom)"""
import sys
import admin_console_manage as acm
import admin_login_protocol as alp
import adobe_jil as jil
import console_children

jil.PROXIES = None  # ★验证走直连(不走代理):母号登录 authorize2 步对轮换IP敏感,且排除代理高并发过载
cfg, cs = acm._load_consoles()
targets = sys.argv[1:] or ["clinebarclay", "ellisisom"]

for key in targets:
    c = next((x for x in cs if key.lower() in (x.get("admin_email") or "").lower()), None)
    print("=" * 56)
    if not c:
        print("没找到母号:", key)
        continue
    print("验证母号:", c["admin_email"])
    tok = alp.protocol_login(c)
    if not tok:
        print("  ❌母号协议登录失败(拿不到token)")
        continue
    org, prod = c["org_id"], c["product_id"]
    try:
        users = jil.list_product_users(org, prod, tok)
    except Exception as e:
        print("  ❌JIL list_product_users 失败:", str(e)[:90])
        continue
    me = (c["admin_email"] or "").lower()
    org_subs = {u["email"].lower() for u in users if u["email"].lower() != me}
    local = console_children.get_children(c) or []
    local_subs = {(ch.get("email") or "").lower() for ch in local if ch.get("email")}
    inter = local_subs & org_subs
    print("  JIL org 实际成员 %d | 其中子号 %d | 本地清单子号 %d" % (len(users), len(org_subs), len(local_subs)))
    print("  ★本地子号真在 org 里的: %d / %d" % (len(inter), len(local_subs)))
    notin = sorted(local_subs - org_subs)
    if notin:
        print("  ⚠️ 本地有、但 org 里【没有】(没真加进去,这些永远10): %d个 %s" % (len(notin), notin[:6]))
    else:
        print("  ✅ 本地子号【全部】在 org 里 → 真加进去了!现在全10 = 权益传播延迟,等会儿就有4000")
