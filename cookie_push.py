# -*- coding: utf-8 -*-
"""Async cookie push support for exported Firefly accounts."""
import hashlib
import json
import os
import tempfile
import threading
import time
import uuid

import requests

import console_children
from safe_file_io import exclusive_file_lock

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "push_config.json")
RECORDS_FILE = os.path.join(BASE_DIR, "push_records.json")
COOKIE_FILE = os.path.join(BASE_DIR, "firefly_adobe2api_cookies.json")

DEFAULT_CONFIG = {
    "enabled": False,
    "api_url": "",
    "api_key": "",
    "account_type": "credit",
    "groups": {},
}


def _atomic_write_json(path, data):
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=BASE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: data.get(k) for k in cfg.keys() if k in data})
    cfg["enabled"] = bool(cfg.get("enabled"))
    cfg["api_url"] = str(cfg.get("api_url") or "").strip()
    cfg["api_key"] = str(cfg.get("api_key") or "").strip()
    cfg["account_type"] = str(cfg.get("account_type") or "credit").strip() or "credit"
    groups = cfg.get("groups") if isinstance(cfg.get("groups"), dict) else {}
    clean_groups = {}
    for name, item in groups.items():
        if not isinstance(item, dict):
            continue
        group = str(name or "").strip()
        if not group:
            continue
        clean_groups[group] = {
            "enabled": bool(item.get("enabled")),
            "api_url": str(item.get("api_url") or "").strip(),
            "api_key": str(item.get("api_key") or "").strip(),
            "account_type": str(item.get("account_type") or "credit").strip() or "credit",
        }
    cfg["groups"] = clean_groups
    return cfg


def save_config(data):
    old = load_config()
    old_group = old.get("groups", {}).get(str(data.get("group") or "").strip(), {})
    group = str(data.get("group") or "").strip()
    cfg = {k: old.get(k, v) for k, v in DEFAULT_CONFIG.items()}
    cfg["groups"] = old.get("groups", {})
    if group:
        cfg["groups"][group] = {
            "enabled": bool(data.get("enabled")),
            "api_url": str(data.get("api_url") or "").strip(),
            "api_key": str(data.get("api_key") or "").strip() or old_group.get("api_key", ""),
            "account_type": str(data.get("account_type") or "credit").strip() or "credit",
        }
    else:
        cfg.update({
            "enabled": bool(data.get("enabled")),
            "api_url": str(data.get("api_url") or "").strip(),
            "api_key": str(data.get("api_key") or "").strip() or old.get("api_key", ""),
            "account_type": str(data.get("account_type") or "credit").strip() or "credit",
        })
    with exclusive_file_lock(CONFIG_FILE):
        _atomic_write_json(CONFIG_FILE, cfg)
    return cfg


def public_config():
    cfg = load_config()
    return {
        "enabled": cfg["enabled"],
        "api_url": cfg["api_url"],
        "api_key": cfg["api_key"],
        "api_key_set": bool(cfg["api_key"]),
        "account_type": cfg["account_type"],
        "groups": {
            name: {
                "enabled": item.get("enabled", False),
                "api_url": item.get("api_url", ""),
                "api_key": item.get("api_key", ""),
                "api_key_set": bool(item.get("api_key")),
                "account_type": item.get("account_type", "credit"),
            }
            for name, item in cfg.get("groups", {}).items()
        },
    }


def config_for_group(group_name):
    cfg = load_config()
    group = str(group_name or "").strip()
    if group and group in cfg.get("groups", {}):
        item = cfg["groups"][group]
        return {
            "enabled": bool(item.get("enabled")),
            "api_url": item.get("api_url", ""),
            "api_key": item.get("api_key", ""),
            "account_type": item.get("account_type", "credit"),
            "group": group,
        }
    cfg["group"] = ""
    return cfg


def _load_records_unlocked():
    try:
        with open(RECORDS_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict):
            records = data.get("records")
        else:
            records = data
        if isinstance(records, list):
            return records
    except Exception:
        pass
    return []


def load_records(limit=200):
    with exclusive_file_lock(RECORDS_FILE):
        records = _load_records_unlocked()
    records = sorted(records, key=lambda x: int((x or {}).get("ts") or 0), reverse=True)
    return records[: max(1, int(limit or 200))]


def clear_records():
    with exclusive_file_lock(RECORDS_FILE):
        _atomic_write_json(RECORDS_FILE, {"records": []})


def _append_record(record):
    with exclusive_file_lock(RECORDS_FILE):
        records = _load_records_unlocked()
        records.append(record)
        records = records[-1000:]
        _atomic_write_json(RECORDS_FILE, {"records": records})


def _normalize_cookie_entries(data):
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        data = data.get("items")
    elif isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        email = str(item.get("name") or item.get("email") or "").strip()
        cookie = str(item.get("cookie") or "").strip()
        if email and cookie:
            out.append({"email": email, "cookie": cookie})
    return out


def _load_cookie_map():
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        data = []
    return {row["email"].lower(): row for row in _normalize_cookie_entries(data)}


def collect_console_accounts(console_email):
    children = console_children.get_children(console_email)
    cookie_map = _load_cookie_map()
    accounts = []
    for child in children:
        email = str((child or {}).get("email") or "").strip()
        if not email:
            continue
        row = cookie_map.get(email.lower())
        if row and row.get("cookie"):
            accounts.append({"email": email, "cookie": row["cookie"]})
    return accounts, len(children or [])


# 下游单次最多接收的账号数（旧代码 accounts[:10] 的来源）。超出会分多次发，绝不静默丢号。
_PUSH_CHUNK = 10


def _accounts_hash(accounts):
    """对 (email, cookie) 集合做稳定 hash，用于幂等去重。"""
    h = hashlib.sha1()
    for a in sorted(accounts or [], key=lambda x: str((x or {}).get("email") or "").lower()):
        h.update(str((a or {}).get("email") or "").lower().encode("utf-8", "ignore"))
        h.update(b"\t")
        h.update(str((a or {}).get("cookie") or "").encode("utf-8", "ignore"))
        h.update(b"\n")
    return h.hexdigest()


def _last_accepted_hash(console_email):
    """该母号上一次'已接收'推送的内容 hash；用于判断这次内容有没有变。"""
    key = str(console_email or "")
    with exclusive_file_lock(RECORDS_FILE):
        records = _load_records_unlocked()
    for rec in sorted(records, key=lambda x: int((x or {}).get("ts") or 0), reverse=True):
        if str((rec or {}).get("console") or "") == key and (rec or {}).get("status") == "accepted":
            return str((rec or {}).get("ck_hash") or "")
    return ""


def _push_now(console_email, accounts, expected_count=None, cfg=None, record_id=None, force=False, ignore_master=False):
    cfg = cfg or load_config()
    ck_hash = _accounts_hash(accounts or [])
    record = {
        "id": record_id or str(uuid.uuid4()),
        "ts": int(time.time()),
        "console": str(console_email or ""),
        "expected_count": int(expected_count or len(accounts or [])),
        "actual_count": len(accounts or []),
        "sent_count": 0,
        "status": "pending",
        "http_status": None,
        "job_id": "",
        "error": "",
        "ck_hash": ck_hash,
        "api_url": cfg.get("api_url", ""),
        "account_type": cfg.get("account_type", "credit"),
        "group": cfg.get("group", ""),
    }
    # ★★推送总开关(全局,唯一闸):enabled=false → 自动监控/换号/全流程/导出后自动推 一切都拦(连 force 也拦)。
    #   只有手动【推送】按钮(ignore_master=True)是用户显式点的,不受总闸限制。
    if not ignore_master and not load_config().get("enabled"):
        record.update({"status": "skipped", "error": "推送总开关已关(全局),未推 adobe2api"})
        _append_record(record)
        return record
    if not accounts:
        record.update({"status": "skipped", "error": "no exported cookies for this console"})
        _append_record(record)
        return record
    if not cfg.get("api_url") or not cfg.get("api_key"):
        record.update({"status": "skipped", "error": "push api url/key not configured"})
        _append_record(record)
        return record
    # 幂等去重：和上次"已接收"的内容完全一致 → 跳过，避免重复灌下游
    if not force and ck_hash and ck_hash == _last_accepted_hash(console_email):
        record.update({"status": "unchanged", "error": "与上次推送内容相同，已跳过（避免重复推送）"})
        _append_record(record)
        return record

    headers = {"Content-Type": "application/json", "X-Push-API-Key": cfg["api_key"]}
    chunks = [accounts[i:i + _PUSH_CHUNK] for i in range(0, len(accounts), _PUSH_CHUNK)]
    sent = 0
    sent_accounts = []   # 实际推送成功(accepted)的号 → 计入已售台账
    job_ids = []
    statuses = []
    errors = []
    last_http = None
    for chunk in chunks:
        payload = {
            "note": f"{console_email} / FF_team",
            "account_type": cfg.get("account_type") or "credit",
            "accounts": chunk,
        }
        try:
            # (连接10s, 读取180s)：服务器导入+校验一批CK常>20s，读超时太短会把成功的推送误判失败
            # ★proxies={http/https:None} 强制直连,绕开本机 HTTP_PROXY 环境变量(VPN/clash)——
            #   推 adobe2api(公网IP)本就该直连,走VPN代理会"Invalid HTTP request received"(尤其TUN关了代理死)
            resp = requests.post(cfg["api_url"], headers=headers, json=payload, timeout=(10, 180),
                                 proxies={"http": None, "https": None})
            last_http = resp.status_code
            try:
                body = resp.json()
            except Exception:
                body = {"text": resp.text[:500]}
            jid = str(body.get("job_id") or "")
            if resp.status_code == 200 and body.get("status") == "accepted":
                statuses.append("accepted")
                sent += len(chunk)
                sent_accounts.extend(chunk)
                if jid:
                    job_ids.append(jid)
            else:
                statuses.append("failed")
                errors.append(json.dumps(body, ensure_ascii=False)[:300])
        except Exception as exc:
            statuses.append("failed")
            errors.append(str(exc)[:300])
    record["http_status"] = last_http
    record["job_id"] = ",".join(job_ids)
    record["sent_count"] = sent
    if statuses and all(s == "accepted" for s in statuses):
        record["status"] = "accepted"
    elif sent > 0:
        record["status"] = "partial"
        record["error"] = "; ".join(errors)[:800]
    else:
        record["status"] = "failed"
        record["error"] = "; ".join(errors)[:800]
    # ★推送到 adobe2api 成功的号 = 已出库 → 计入"已售":记台账(日期+基线4000) + 写 exported(前端显示已售/防重复导)
    if sent_accounts:
        try:
            import _export_a2a
            _ems = [e for e in (str(a.get("email") or "").strip() for a in sent_accounts) if e]
            if _ems:
                _export_a2a.record_sold(_ems)   # baseline 默认4000(adobe2api 推的多是企业4000档)
                _seen = _export_a2a.load_exported()
                _new = [e for e in _ems if e.lower() not in _seen]
                if _new:
                    with open(_export_a2a.EXPORTED_FILE, "a", encoding="utf-8") as _f:
                        for _e in _new:
                            _f.write(_e.lower() + "\n")
        except Exception:
            pass
    _append_record(record)
    return record


def push_console_async(console_email, emit=None, force=False, group="", only_emails=None):
    """only_emails 给定时只推这些子号(单号/子集导出用),不重推整个母号→避免没动的老号被重复导入下游。"""
    cfg = config_for_group(group)
    if not force and not cfg.get("enabled"):
        return False
    only = {str(e).strip().lower() for e in only_emails if str(e).strip()} if only_emails else None

    def worker():
        accounts, expected = collect_console_accounts(console_email)
        if only is not None:
            accounts = [a for a in accounts if str(a.get("email") or "").strip().lower() in only]
            expected = len(accounts)
        rec = _push_now(console_email, accounts, expected_count=expected, cfg=cfg, force=force)
        if emit:
            st = rec["status"]
            if st == "accepted":
                emit(f"[推送API] {console_email} 已接收：{rec.get('sent_count', rec['actual_count'])}/{rec['expected_count']} job_id={rec.get('job_id') or '-'}")
            elif st == "partial":
                emit(f"[推送API] {console_email} 部分成功：{rec.get('sent_count', 0)}/{rec['actual_count']} {rec.get('error') or ''}")
            elif st == "unchanged":
                emit(f"[推送API] {console_email} 跳过：内容与上次相同，未重复推送")
            elif st == "skipped":
                emit(f"[推送API] {console_email} 跳过：{rec['error']}")
            else:
                emit(f"[推送API] {console_email} 失败：HTTP {rec.get('http_status') or '-'} {rec.get('error') or ''}")

    threading.Thread(target=worker, daemon=True).start()
    return True


def push_console_sync(console_email, group="", force=False, ignore_master=False):
    # 推送总开关由 _push_now 统一把关(ignore_master=True 才绕过总闸,只给手动【推送】按钮)。
    cfg = config_for_group(group)
    accounts, expected = collect_console_accounts(console_email)
    return _push_now(console_email, accounts, expected_count=expected, cfg=cfg, force=force, ignore_master=ignore_master)
