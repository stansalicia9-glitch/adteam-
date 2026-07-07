# -*- coding: utf-8 -*-
"""验证:浏览器版 firefly_login_extract 导一个没激活的子号,会自动点"加入团队"激活+选企业→4000。"""
import re, subprocess, sys
import admin_console_manage as acm
import console_children

cfg, cs = acm._load_consoles()
key = sys.argv[1] if len(sys.argv) > 1 else "clinebarclay"
c = next((x for x in cs if key.lower() in (x.get("admin_email") or "").lower()), None)
ch = console_children.get_children(c) or []
target = next((x for x in ch if "ashley" not in (x.get("email") or "").lower()), ch[0] if ch else None)
email = target["email"]
# 取带 RT 的完整行(console_children raw 可能只有 email----password,拿码需要 RT)
full = str(target.get("raw") or "")
for l in open("added_accounts.txt", encoding="utf-8", errors="replace"):
    if email.lower() in l.lower():
        full = l.strip()
        break
open("_test_one.txt", "w", encoding="utf-8").write(full + "\n")
print("测试子号(没激活):", email, "| 行里带RT:", "M." in full, flush=True)

subprocess.run([sys.executable, "firefly_login_extract_cookies.py", "--accounts", "_test_one.txt",
                "--headless", "--no-manual", "--manual-timeout", "0", "--workers", "1", "--retry-rounds", "1"])

import _quota
import cookie_push
cm = cookie_push._load_cookie_map()
row = cm.get(email.lower())
if row and row.get("cookie"):
    q = _quota.query_quota(row["cookie"])
    print("★★★ 浏览器版激活后积分:", q.get("available"), "/", q.get("total"), flush=True)
else:
    print("❌ 没导出到 cookie(激活/拿码失败,看上面日志)", flush=True)
