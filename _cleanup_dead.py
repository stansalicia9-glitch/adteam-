# -*- coding: utf-8 -*-
"""删 20 个死/封母号:先备份→从 config / console_children / cookie池 / 已售清单 / sold_ledger 清掉它们及其子号。纯本地。"""
import os, json, shutil, time
import admin_console_manage as acm, console_children
import firefly_register_yescaptcha as fry
import _export_a2a

BASE = os.path.dirname(os.path.abspath(__file__))
DEAD = [e.lower() for e in [
    "FondaWyma907524@hotmail.com", "NewtJuvinall8887@hotmail.com", "DesiBurja81803@hotmail.com",
    "NelieJova6810@hotmail.com", "AriettaMunts75784@hotmail.com", "EduardoParmentier2772@hotmail.com",
    "kitchellboza71@hotmail.com", "millyhoelzel4597@hotmail.com", "navaretteping570@hotmail.com",
    "leonepuleio7906@hotmail.com", "WaltSmedberg5580@hotmail.com", "MaliaCavelli0256@hotmail.com",
    "JensZordan798224@hotmail.com", "ridenourheaton2z5sup@hotmail.com", "WenzelMccollaum3144@hotmail.com",
    "veronvanderroest875@hotmail.com", "BelleGolata24984@hotmail.com", "FateWeyers841494@hotmail.com",
    "DuffJuergens6861@hotmail.com", "lissyzorzi60@hotmail.com"]]

# 1) 备份
stamp = time.strftime("%Y%m%d-%H%M%S")
bdir = os.path.join(BASE, "_bak_" + stamp); os.makedirs(bdir, exist_ok=True)
for fn in ["admin_console_config.json", "console_children.json", "firefly_adobe2api_cookies.json", "exported_accounts.txt", "sold_ledger.json"]:
    p = os.path.join(BASE, fn)
    if os.path.exists(p):
        shutil.copy2(p, os.path.join(bdir, fn))
print("✅ 已备份到 %s" % bdir, flush=True)

# 2) 死母号 + 其子号
cfg, cons = acm._load_consoles()
dead_cons = [c for c in cons if (c.get("admin_email") or c.get("name") or "").lower() in DEAD]
dead_kids = set()
for c in dead_cons:
    sel = c.get("admin_email") or c.get("name") or ""
    for k in console_children.get_children(sel):
        em = (k.get("email") or "").strip().lower()
        if em:
            dead_kids.add(em)
print("死母号 %d 个,名下子号 %d 个" % (len(dead_cons), len(dead_kids)), flush=True)

# 3) cookie 池删子号
entries = fry._load_adobe2api_cookie_entries()
kept = [e for e in entries if str(e.get("name") or "").strip().lower() not in dead_kids]
fry._write_adobe2api_cookie_entries(kept)
print("cookie池: %d → %d (删 %d)" % (len(entries), len(kept), len(entries) - len(kept)), flush=True)

# 4) exported_accounts.txt
exp = _export_a2a.load_exported(); exp2 = exp - dead_kids
if len(exp2) != len(exp):
    with open(_export_a2a.EXPORTED_FILE, "w", encoding="utf-8") as f:
        for e in sorted(exp2):
            f.write(e + "\n")
print("已售清单exported: %d → %d" % (len(exp), len(exp2)), flush=True)

# 5) sold_ledger
led = _export_a2a.load_sold_ledger(); led2 = {k: v for k, v in led.items() if k.lower() not in dead_kids}
if len(led2) != len(led):
    with open(_export_a2a.SOLD_LEDGER, "w", encoding="utf-8") as f:
        json.dump(led2, f, ensure_ascii=False, indent=2)
print("sold_ledger: %d → %d" % (len(led), len(led2)), flush=True)

# 6) console_children 删死母号
cc_path = os.path.join(BASE, "console_children.json")
try:
    cc = json.load(open(cc_path, encoding="utf-8"))
    cm = cc.get("consoles", cc) if isinstance(cc, dict) else {}
    before = len(cm)
    for sel in list(cm.keys()):
        if sel.lower() in DEAD:
            del cm[sel]
    if isinstance(cc, dict) and "consoles" in cc:
        cc["consoles"] = cm
    else:
        cc = cm
    json.dump(cc, open(cc_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("console_children母号: %d → %d" % (before, len(cm)), flush=True)
except Exception as e:
    print("console_children清理异常: %s" % e, flush=True)

# 7) config 删死母号
disk = json.load(open(acm.CONFIG_FILE, encoding="utf-8-sig"))
cl = disk.get("consoles", []); before = len(cl)
disk["consoles"] = [c for c in cl if (c.get("admin_email") or c.get("name") or "").lower() not in DEAD]
json.dump(disk, open(acm.CONFIG_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("config母号: %d → %d (删 %d)" % (before, len(disk["consoles"]), before - len(disk["consoles"])), flush=True)
print("\n✅ 清理完成,剩活母号 %d 个。备份: %s" % (len(disk["consoles"]), bdir), flush=True)
