# -*- coding: utf-8 -*-
"""单母号 JIL 诊断:协议登录 → list_organizations(看org是否归零) → list_product_users。
末尾输出一行机器可解析的 SUMMARY,供 _diag_all_masters.py 批量收集。"""
import sys, os, json, base64
PROXY = os.environ.get('PROXY') or None  # 设了就协议登录+JIL全走代理(adobe_jil的PROXIES也读env PROXY)
import admin_login_protocol as alp
import adobe_jil as jil

idx = int(sys.argv[1]) if len(sys.argv) > 1 else 0
d = json.load(open('admin_console_config.json', encoding='utf-8'))
cs = d if isinstance(d, list) else d.get('consoles') or d.get('admin_consoles') or []
c = cs[idx]
email = c.get('email') or c.get('admin_email')
orgs_n, subs_n, result = -1, -1, "?"
print(f"==== 诊断 idx={idx} {email} | org={c.get('org_id')} prod={c.get('product_id')}", flush=True)

try:
    tok = alp.protocol_login(c, proxy=PROXY)
    if not tok:
        result = "登录失败(拿码失败/限流)"
    else:
        try:
            pl = tok.split('.')[1]; pl += '=' * (-len(pl) % 4)
            claim = json.loads(base64.urlsafe_b64decode(pl))
            print(f"[token] len={len(tok)} scope_head={str(claim.get('scope',''))[:60]}", flush=True)
        except Exception:
            pass
        try:
            orgs = jil.list_organizations(tok); orgs_n = len(orgs)
            print(f"[list_organizations] → {orgs_n} 个: {[o.get('id') for o in orgs][:4]}", flush=True)
            if orgs_n == 0:
                result = "org归零-失效"
            else:
                try:
                    us = jil.list_product_users(c['org_id'], c['product_id'], tok); subs_n = len(us)
                    result = "活"
                    print(f"[list_product_users] → 子号 {subs_n}", flush=True)
                except Exception as e:
                    result = "org在但product403"
                    print(f"[list_product_users] 失败: {str(e)[:80]}", flush=True)
        except Exception as e:
            result = "list_org异常"
            print(f"[list_organizations] 失败: {str(e)[:100]}", flush=True)
except Exception as e:
    result = f"登录异常:{str(e)[:40]}"
    print(f"[协议登录异常] {str(e)[:160]}", flush=True)

print(f"SUMMARY\tidx={idx}\temail={email}\torgs={orgs_n}\tsubs={subs_n}\tresult={result}", flush=True)
