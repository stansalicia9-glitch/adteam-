# -*- coding: utf-8 -*-
"""Current child-account ledger per admin console."""
import json
import os
import tempfile

from safe_file_io import exclusive_file_lock

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "console_children.json")


def _console_key(console_or_email):
    if isinstance(console_or_email, dict):
        value = console_or_email.get("admin_email") or console_or_email.get("name") or ""
    else:
        value = console_or_email
    return str(value or "").strip().lower()


def _load_unlocked():
    try:
        with open(STATE_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("consoles", {})
            return data
    except Exception:
        pass
    return {"version": 1, "consoles": {}}


def _save_unlocked(data):
    data.setdefault("version", 1)
    data.setdefault("consoles", {})
    fd, tmp = tempfile.mkstemp(prefix="console_children_", suffix=".tmp", dir=BASE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def set_children(console_or_email, accounts):
    key = _console_key(console_or_email)
    if not key:
        return
    rows = []
    seen = set()
    for acc in accounts or []:
        email = str((acc or {}).get("email") or "").strip()
        raw = str((acc or {}).get("raw") or "").strip()
        if not email or not raw:
            continue
        lk = email.lower()
        if lk in seen:
            continue
        seen.add(lk)
        rows.append({"email": email, "raw": raw})
    with exclusive_file_lock(STATE_FILE):
        data = _load_unlocked()
        data.setdefault("consoles", {})[key] = rows
        _save_unlocked(data)


def get_children(console_or_email):
    key = _console_key(console_or_email)
    if not key:
        return []
    with exclusive_file_lock(STATE_FILE):
        data = _load_unlocked()
        return list(data.get("consoles", {}).get(key) or [])


def remove_children(console_or_email, emails):
    """从本地子号清单删掉指定 email(死号/幽灵号清理,纯本地、不碰 Adobe 团队)。
    给了 console 就只在该母号下删;没给(空)则遍历所有母号删。返回删掉的个数。"""
    rm = {str(e).strip().lower() for e in (emails or []) if str(e).strip()}
    if not rm:
        return 0
    key = _console_key(console_or_email)
    with exclusive_file_lock(STATE_FILE):
        data = _load_unlocked()
        cons = data.get("consoles", {})
        keys = [key] if key else list(cons.keys())
        n = 0
        for k in keys:
            rows = cons.get(k) or []
            kept = [r for r in rows if str((r or {}).get("email") or "").strip().lower() not in rm]
            if len(kept) != len(rows):
                n += len(rows) - len(kept)
                cons[k] = kept
        if n:
            _save_unlocked(data)
        return n


def all_children():
    with exclusive_file_lock(STATE_FILE):
        data = _load_unlocked()
        out = []
        seen = set()
        for rows in (data.get("consoles") or {}).values():
            for item in rows or []:
                email = str((item or {}).get("email") or "").strip()
                raw = str((item or {}).get("raw") or "").strip()
                if not email or not raw:
                    continue
                lk = email.lower()
                if lk in seen:
                    continue
                seen.add(lk)
                out.append({"email": email, "raw": raw})
        return out


def counts_by_console():
    with exclusive_file_lock(STATE_FILE):
        data = _load_unlocked()
        return {key: len(rows or []) for key, rows in (data.get("consoles") or {}).items()}
