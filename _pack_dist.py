# -*- coding: utf-8 -*-
"""打 FF_protocol【对外分发】包(清敏感)到桌面 FF_protocol_dist + .zip。
保留:代码(.py) + 模板/静态 + python 便携运行时 + requirements。
清掉:所有账密/cookie/邮箱池/已售/导出/日志/swaps临时(.txt/.json/.log/.csv/.har/.db);
     config.json/push_config.json 清空密钥保结构;admin_console_config.json 换空模板。
全 Python(shutil+zipfile),避开 PowerShell robocopy/ZipArchive 的坑。
"""
import os, shutil, zipfile, json

SRC = os.path.dirname(os.path.abspath(__file__))
NAME = "FF_protocol_dist"
DESK = os.path.join(os.path.expanduser("~"), "Desktop")
DST = os.path.join(DESK, NAME)
ZIP = os.path.join(DESK, NAME + ".zip")

SKIP_DIRS = {"admin_profile", "firefly_debug", "__pycache__", ".git", "_accounts",
             "current_child_exports", "firefly_browser_profiles", "ms-playwright",
             ".pytest_cache", ".idea", ".vscode"}
KEEP_DATA = {"requirements.txt"}
CONFIG_TPL = {"admin_console_config.json", "config.json", "push_config.json"}


def is_data_secret(fn):
    low = fn.lower()
    if fn in KEEP_DATA:
        return False
    ext = low.rsplit(".", 1)[-1] if "." in low else ""
    if low.endswith("_shot.png") or low == "_panel_shot.png":
        return True
    return ext in ("txt", "json", "log", "csv", "har", "db", "jsonl", "bak")


def clean_config(d):
    if not isinstance(d, dict):
        return d
    SENS = ("key", "token", "password", "secret", "proxy", "api_url", "client_id", "client_secret", "cookie")
    for k in list(d.keys()):
        kl = k.lower()
        v = d[k]
        if any(s in kl for s in SENS) or k in ("cards", "teams", "firefly_outlook_accounts", "default_address"):
            d[k] = "" if isinstance(v, str) else ([] if isinstance(v, list) else ({} if isinstance(v, dict) else v))
    return d


# 1) 同步顶层(排除大/调试目录)
if os.path.exists(DST):
    shutil.rmtree(DST, ignore_errors=True)
os.makedirs(DST)
for item in os.listdir(SRC):
    if item in SKIP_DIRS:
        continue
    s = os.path.join(SRC, item)
    d = os.path.join(DST, item)
    if os.path.isdir(s):
        ig = shutil.ignore_patterns("__pycache__") if item == "python" else shutil.ignore_patterns("__pycache__", "_cdp_*")
        shutil.copytree(s, d, ignore=ig)
    else:
        if is_data_secret(item):
            continue
        shutil.copy2(s, d)

# 2) 递归清残留敏感(不动 python 运行时)
for root, dirs, files in os.walk(DST):
    if "python" in root.replace(DST, "").split(os.sep):
        continue
    for f in files:
        if is_data_secret(f):
            try:
                os.remove(os.path.join(root, f))
            except Exception:
                pass

# 3) 写清空模板
json.dump({"consoles": [], "target_seats_per_console": 9, "proxy": ""},
          open(os.path.join(DST, "admin_console_config.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
for fn in ("config.json", "push_config.json"):
    src = os.path.join(SRC, fn)
    if os.path.exists(src):
        try:
            d = json.load(open(src, encoding="utf-8-sig"))
            json.dump(clean_config(d), open(os.path.join(DST, fn), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        except Exception:
            pass

# 4) 补 playwright _cdp*(防 _cdp_* 误删 _cdp_session.py)
pysrc = os.path.join(SRC, "python")
pydst = os.path.join(DST, "python")
cdp = 0
for root, dirs, files in os.walk(pysrc):
    if "playwright" not in root.replace("\\", "/"):
        continue
    for f in files:
        if f.startswith("_cdp"):
            rel = os.path.relpath(os.path.join(root, f), pysrc)
            dd = os.path.join(pydst, rel)
            if not os.path.exists(dd):
                os.makedirs(os.path.dirname(dd), exist_ok=True)
                shutil.copy2(os.path.join(root, f), dd)
                cdp += 1

# 5) zip(正斜杠 entry, 顶层 NAME 前缀, Fastest)
if os.path.exists(ZIP):
    os.remove(ZIP)
cnt = 0
with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
    for root, dirs, files in os.walk(DST):
        for f in files:
            fp = os.path.join(root, f)
            arc = NAME + "/" + os.path.relpath(fp, DST).replace("\\", "/")
            z.write(fp, arc)
            cnt += 1

# 6) 验证
leak = []
for root, dirs, files in os.walk(DST):
    if "python" in root.replace(DST, "").split(os.sep):
        continue
    for f in files:
        if is_data_secret(f) and f not in CONFIG_TPL:
            leak.append(os.path.relpath(os.path.join(root, f), DST))
cfg_leak = ""
try:
    c = json.load(open(os.path.join(DST, "config.json"), encoding="utf-8"))
    for k in ("yescaptcha_api_key", "capsolver_api_key", "SUB2API_TOKEN", "proxy", "firefly_outlook_accounts"):
        if c.get(k):
            cfg_leak += " " + k
except Exception:
    pass
print("✅ 打包完成", flush=True)
print("  文件夹:", DST, flush=True)
print("  zip:", ZIP, "(%.1f MB)" % (os.path.getsize(ZIP) / 1024 / 1024), "| entry", cnt, "| 补_cdp", cdp, flush=True)
print("  敏感残留:", leak if leak else "无 ✓", flush=True)
print("  config模板残留密钥:", cfg_leak.strip() if cfg_leak.strip() else "无 ✓(已清)", flush=True)
print("  py文件计数:", sum(1 for r, d2, fs in os.walk(DST) if "python" not in r.replace(DST, "").split(os.sep) for f in fs if f.endswith(".py")), flush=True)
