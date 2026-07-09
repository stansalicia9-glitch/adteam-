# -*- coding: utf-8 -*-
"""抓"加入团队"激活请求:用 console_children 一个【没激活、密码对】的子号,dump 它走加入团队的关键请求。"""
import subprocess, sys, os
import admin_console_manage as acm
import console_children

cfg, cs = acm._load_consoles()
c = next(x for x in cs if "clinebarclay" in (x.get("admin_email") or "").lower())
ch = console_children.get_children(c) or []
# 用确定【没激活】的子号(boe5k4xeo 测出 filtered_profiles 只有个人 profile)抓"加入团队"
_want = ("jason", "lisa")
target = next((x for x in ch if any(u in (x.get("email") or "").lower() for u in _want)), ch[0])
open("_cap_one.txt", "w", encoding="utf-8").write(str(target.get("raw") or "") + "\n")
print("用 console_children 没激活子号(密码对):", target["email"], flush=True)

env = dict(os.environ)
env["FF_DUMP_REQ"] = "1"
env["PYTHONIOENCODING"] = "utf-8"
subprocess.run([sys.executable, "-u", "firefly_login_extract_cookies.py", "--accounts", "_cap_one.txt",
                "--headless", "--no-manual", "--manual-timeout", "0", "--workers", "1", "--retry-rounds", "1",
                "--proxy", "http://USER387969-zone-custom-region-US:911ae9@us.rrp.bestgo.work:10000"], env=env)
print("✅ dump 完", flush=True)
