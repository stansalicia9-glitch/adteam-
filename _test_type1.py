# -*- coding: utf-8 -*-
"""实测 TYPE1:对一个母号删1旧子号+加1新子号(已改TYPE1),导出看积分是不是4000。"""
import sys, subprocess
import admin_console_manage as acm
import console_children

key = sys.argv[1] if len(sys.argv) > 1 else "emilia"
cfg, cs = acm._load_consoles()
c = next((x for x in cs if key.lower() in (x.get("admin_email") or "").lower()), None)
email = c["admin_email"]
ch = console_children.get_children(c) or []
if not ch:
    print("该母号无子号清单"); sys.exit(1)
old = ch[0]["email"]
print("TYPE1 实测: 母号 %s | 删1旧子号 %s + 加1新(TYPE1) → 等传播45s → 导出看积分" % (email, old), flush=True)
subprocess.run([sys.executable, "admin_jil_swap.py", "--console", email, "--old", old,
                "--then-extract", "--console-workers", "1", "--export-delay", "45"])
