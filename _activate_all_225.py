# -*- coding: utf-8 -*-
"""清 cookie 库 → 用所有 console_children(225子号)跑浏览器版导出(自动加入团队激活+选企业→4000)→ 按母号推 adobe2api。"""
import subprocess, sys, os, shutil, json
import admin_console_manage as acm
import console_children

cfg, cs = acm._load_consoles()
COOKIE = "firefly_adobe2api_cookies.json"

# 1) 备份 + 清空 cookie 库(按用户要求)
if os.path.exists(COOKIE):
    if not os.path.exists(COOKIE + ".bak_before225"):
        shutil.copy2(COOKIE, COOKIE + ".bak_before225")
    json.dump({"items": []}, open(COOKIE, "w", encoding="utf-8"), ensure_ascii=False)
    print("✅ 已清空 cookie 库(备份 .bak_before225)", flush=True)

# 2) 收集所有 225 子号 raw
rows = []
for c in cs:
    for ch in (console_children.get_children(c) or []):
        raw = str(ch.get("raw") or "").strip()
        if raw:
            rows.append(raw)
open("_all225.txt", "w", encoding="utf-8").write("\n".join(rows) + "\n")
print(f"==== 共 {len(rows)} 子号,浏览器版导出(自动激活+4000),并发5 ====", flush=True)

# 3) 浏览器版导出(自动点加入团队激活+选企业)
subprocess.run([sys.executable, "firefly_login_extract_cookies.py", "--accounts", "_all225.txt",
                "--headless", "--no-manual", "--manual-timeout", "0", "--workers", "5", "--retry-rounds", "2"])

# 4) 按母号推 adobe2api
import cookie_push
import _quota
print("\n==== 导出完成,按母号推 adobe2api + 抽查积分 ====", flush=True)
total_push = 0
for c in cs:
    sel = c.get("admin_email")
    accts, cnt = cookie_push.collect_console_accounts(sel)
    if not accts:
        continue
    rec = cookie_push._push_now(sel, accts, force=True)
    sc = rec.get("sent_count") or 0
    total_push += sc
    # 抽查第一个的积分
    q = {}
    try:
        q = _quota.query_quota(accts[0]["cookie"])
    except Exception:
        pass
    print(f"  {sel}: 推 {rec.get('status')} {sc}/{len(accts)} | 抽查积分 {q.get('total')}", flush=True)
print(f"\n✅ 全部完成:共推 {total_push} 个子号到 adobe2api", flush=True)
