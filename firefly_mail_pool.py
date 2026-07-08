import json
import os
import re
import threading
import time


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "firefly_mail_pool_state.json")
DEFAULT_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
STATUS_VALUES = {"firefly", "available", "success", "failed", "deprecated", "in_use", "registered", "done"}

_LOCK = threading.RLock()


def _now():
    return int(time.time())


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    last_error = None
    for attempt in range(10):
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            os.replace(tmp_path, path)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.2 * (attempt + 1))
    # Windows can deny atomic replace when another process has the target open.
    # Fall back to an in-place write so a stale reader cannot block the pool.
    for attempt in range(5):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.5 * (attempt + 1))
    raise last_error


def load_config():
    return _load_json(CONFIG_PATH, {})


def save_config(cfg):
    _write_json(CONFIG_PATH, cfg)


def load_state():
    state = _load_json(STATE_PATH, {})
    if not isinstance(state, dict):
        state = {}
    state.setdefault("emails", {})
    state.setdefault("cursor", 0)
    return state


def save_state(state):
    state.setdefault("emails", {})
    state.setdefault("cursor", 0)
    _write_json(STATE_PATH, state)


def normalize_account(raw):
    def looks_status(value):
        return str(value or "").strip().lower() in STATUS_VALUES

    def looks_client_id(value):
        text = str(value or "").strip()
        return bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", text))

    def looks_refresh_token(value):
        text = str(value or "").strip()
        if not text or looks_status(text) or looks_client_id(text):
            return False
        return text.startswith("M.") or len(text) > 40 or "$" in text

    if isinstance(raw, dict):
        email = str(raw.get("email") or raw.get("mail") or raw.get("username") or "").strip()
        if not email:
            return None
        password = str(raw.get("password") or raw.get("email_password") or raw.get("pass") or "").strip()
        email_password = str(raw.get("email_password") or raw.get("mail_password") or raw.get("mailToken") or "").strip()
        client_id = str(raw.get("client_id") or raw.get("clientId") or DEFAULT_CLIENT_ID).strip()
        refresh_token = str(raw.get("refresh_token") or raw.get("refreshToken") or raw.get("token") or "").strip()
        status = str(raw.get("status") or "").strip()
        line = str(raw.get("raw") or "").strip()
        if not line:
            line = "----".join([email, password, client_id if refresh_token else email_password, refresh_token or status])
        return {
            "email": email,
            "password": password,
            "email_password": email_password,
            "client_id": client_id,
            "refresh_token": refresh_token,
            "status": status,
            "raw": line,
        }

    line = str(raw or "").strip()
    if not line or line.startswith("#"):
        return None
    parts = [p.strip() for p in line.split("----")]
    email = parts[0] if parts else ""
    if not email or "@" not in email:
        return None
    password = parts[1] if len(parts) > 1 else ""
    client_id = DEFAULT_CLIENT_ID
    refresh_token = ""
    email_password = ""
    status = ""
    third = parts[2] if len(parts) > 2 else ""
    fourth = parts[3] if len(parts) > 3 else ""
    fifth = parts[4] if len(parts) > 4 else ""

    if len(parts) > 3:
        if looks_status(fourth):
            status = fourth
            if looks_client_id(third):
                client_id = third
            else:
                email_password = third
        else:
            if looks_client_id(third):
                client_id = third
                refresh_token = fourth
            elif looks_refresh_token(fourth):
                email_password = third
                refresh_token = fourth
            else:
                email_password = third
                status = fourth if looks_status(fourth) else ""
    elif third:
        if looks_status(third):
            status = third
        elif looks_client_id(third):
            client_id = third
        elif looks_refresh_token(third):
            refresh_token = third
        else:
            email_password = third

    if fifth and looks_status(fifth):
        status = fifth
    return {
        "email": email,
        "password": password,
        "email_password": email_password,
        "client_id": client_id,
        "refresh_token": refresh_token,
        "status": status,
        "raw": line,
    }


def parse_accounts_text(text):
    text = str(text or "").strip()
    if not text:
        return []

    parsed = None
    if text[:1] in "[{":
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None

    raw_items = []
    if isinstance(parsed, list):
        raw_items = parsed
    elif isinstance(parsed, dict):
        for key in ("firefly_outlook_accounts", "accounts", "items", "data"):
            value = parsed.get(key)
            if isinstance(value, list):
                raw_items = value
                break
        if not raw_items:
            raw_items = [parsed]
    else:
        raw_items = [line for line in re.split(r"[\r\n]+", text) if line.strip()]

    records = []
    seen = set()
    for item in raw_items:
        record = normalize_account(item)
        if not record:
            continue
        key = record["email"].lower()
        if key in seen:
            continue
        seen.add(key)
        records.append(record)
    return records


def _records_by_email(raw_accounts):
    records = {}
    order = []
    for raw in raw_accounts or []:
        record = normalize_account(raw)
        if not record:
            continue
        key = record["email"].lower()
        if key not in records:
            order.append(key)
        records[key] = record
    return records, order


def import_accounts(records, mode="append"):
    with _LOCK:
        cfg = load_config()
        current_records, current_order = _records_by_email(cfg.get("firefly_outlook_accounts") or [])
        incoming_records, incoming_order = _records_by_email([r.get("raw", r) for r in records])

        if mode == "replace":
            merged = incoming_records
            order = incoming_order
        else:
            merged = dict(current_records)
            order = list(current_order)
            for key in incoming_order:
                if key not in merged:
                    order.append(key)
                merged[key] = incoming_records[key]

        cfg["firefly_outlook_accounts"] = [merged[key]["raw"] for key in order if key in merged]
        save_config(cfg)

        state = load_state()
        email_state = state.setdefault("emails", {})
        if mode == "replace":
            email_state = {k: v for k, v in email_state.items() if k in merged}
            state["emails"] = email_state
            state["cursor"] = 0
        for key in incoming_order:
            record = incoming_records[key]
            item = email_state.setdefault(key, {})
            if "outlook" in str(key).lower():   # ★outlook域子号出图必408(实测),导入即剔除、绝不入可用池;只用hotmail
                item["status"] = "deprecated"
                item["reason"] = "outlook域出图408,自动剔除(只用hotmail)"
                item.setdefault("created_at", _now())
                item["updated_at"] = _now()
                continue
            if not record.get("password"):
                item["status"] = "failed"
                item["reason"] = "missing Adobe account password"
            elif item.get("reason") in {"missing refresh_token", "missing Adobe account password"}:
                item["status"] = "available"
                item["reason"] = ""
            else:
                item.setdefault("status", "available")
                item.setdefault("reason", "")
            item.setdefault("created_at", _now())
            item["updated_at"] = _now()
        save_state(state)

        return {
            "total": len(order),
            "imported": len(incoming_order),
            "mode": mode,
        }


def delete_accounts(emails=None, mode="selected"):
    with _LOCK:
        cfg = load_config()
        records, order = _records_by_email(cfg.get("firefly_outlook_accounts") or [])
        state = load_state()
        email_state = state.setdefault("emails", {})

        if mode == "all":
            deleted = len(order)
            cfg["firefly_outlook_accounts"] = []
            state["emails"] = {k: v for k, v in email_state.items() if k not in records}
            state["cursor"] = 0
            save_config(cfg)
            save_state(state)
            return deleted

        targets = {
            str(email or "").strip().lower()
            for email in (emails or [])
            if str(email or "").strip()
        }
        if not targets:
            return 0

        kept_order = [key for key in order if key not in targets]
        deleted = len(order) - len(kept_order)
        if not deleted:
            return 0

        cfg["firefly_outlook_accounts"] = [
            records[key]["raw"] for key in kept_order if key in records
        ]
        for key in targets:
            email_state.pop(key, None)
        state["cursor"] = 0 if not kept_order else int(state.get("cursor") or 0) % len(kept_order)
        save_config(cfg)
        save_state(state)
        return deleted


def has_account(email):
    key = str(email or "").strip().lower()
    if not key:
        return False
    cfg = load_config()
    records, _ = _records_by_email(cfg.get("firefly_outlook_accounts") or [])
    return key in records


def list_accounts(limit=None):
    with _LOCK:
        try:
            limit = int(limit or 0)
        except Exception:
            limit = 0
        limit = max(0, limit)
        cfg = load_config()
        records, order = _records_by_email(cfg.get("firefly_outlook_accounts") or [])
        state = load_state()
        email_state = state.setdefault("emails", {})
        items = []
        stats = {
            "total": len(order),
            "available": 0,
            "in_use": 0,
            "deprecated": 0,
            "success": 0,
            "failed": 0,
        }
        for key in order:
            record = records[key]
            meta = email_state.get(key) or {}
            status = str(meta.get("status") or "available")
            if status not in stats:
                stats[status] = 0
            stats[status] += 1
            if not limit or len(items) < limit:
                items.append({
                    "email": record["email"],
                    "status": status,
                    "reason": meta.get("reason") or "",
                    "updated_at": meta.get("updated_at") or "",
                })
        return {"stats": stats, "items": items}


def available_count():
    data = list_accounts()
    return int(data.get("stats", {}).get("available") or 0)


def available_accounts(limit=0):
    """返回当前可验/可用的池账号(排除 deprecated/success/failed/in_use)，供验活用。"""
    with _LOCK:
        cfg = load_config()
        records, order = _records_by_email(cfg.get("firefly_outlook_accounts") or [])
        state = load_state()
        email_state = state.get("emails", {})
        out = []
        for key in order:
            meta = email_state.get(key, {})
            status = str(meta.get("status") or "available")
            if status in ("deprecated", "success", "failed", "in_use"):
                continue
            rec = records.get(key) or {}
            if str(rec.get("email") or "").strip():
                out.append(dict(rec))
                if limit and len(out) >= limit:
                    break
        return out


def set_status(email, status, reason=""):
    """把某邮箱标成指定状态(验活用：死号标 deprecated → acquire 自动跳过)。"""
    ek = str(email or "").strip().lower()
    if not ek:
        return False
    with _LOCK:
        cfg = load_config()
        records, order = _records_by_email(cfg.get("firefly_outlook_accounts") or [])
        target = None
        for k in order:
            if str((records.get(k) or {}).get("email") or "").strip().lower() == ek:
                target = k
                break
        if target is None:
            target = ek
        state = load_state()
        meta = state.setdefault("emails", {}).setdefault(target, {})
        meta["status"] = status
        if reason:
            meta["reason"] = reason
        meta["updated_at"] = _now()
        save_state(state)
        return True


def acquire_account(stale_seconds=1800):
    with _LOCK:
        cfg = load_config()
        records, order = _records_by_email(cfg.get("firefly_outlook_accounts") or [])
        if not order:
            return None

        state = load_state()
        email_state = state.setdefault("emails", {})
        cursor = int(state.get("cursor") or 0)
        now = _now()
        skip_statuses = {"deprecated", "success", "failed"}

        for offset in range(len(order)):
            idx = (cursor + offset) % len(order)
            key = order[idx]
            record = records[key]
            meta = email_state.setdefault(key, {})
            if not record.get("password"):
                meta["status"] = "failed"
                meta["reason"] = "missing Adobe account password"
                meta["updated_at"] = now
                continue
            status = str(meta.get("status") or "available")
            if status == "in_use" and now - int(meta.get("leased_at") or 0) > stale_seconds:
                status = "available"
                meta["status"] = "available"
                meta["reason"] = "stale lease reset"
            if status in skip_statuses or status == "in_use":
                continue
            meta["status"] = "in_use"
            meta["reason"] = "leased for registration"
            meta["leased_at"] = now
            meta["updated_at"] = now
            state["cursor"] = idx + 1
            save_state(state)
            return record

        save_state(state)
        return None


def acquire_accounts(count, exclude_emails=None, reason="leased for team add", stale_seconds=1800):
    try:
        count = int(count or 0)
    except Exception:
        count = 0
    if count <= 0:
        return []
    exclude = {
        str(email or "").strip().lower()
        for email in (exclude_emails or [])
        if str(email or "").strip()
    }
    with _LOCK:
        cfg = load_config()
        records, order = _records_by_email(cfg.get("firefly_outlook_accounts") or [])
        if not order:
            return []

        state = load_state()
        email_state = state.setdefault("emails", {})
        cursor = int(state.get("cursor") or 0)
        now = _now()
        skip_statuses = {"deprecated", "success", "failed"}
        picked = []

        for offset in range(len(order)):
            idx = (cursor + offset) % len(order)
            key = order[idx]
            if key in exclude:
                continue
            record = records[key]
            meta = email_state.setdefault(key, {})
            if not record.get("password"):
                meta["status"] = "failed"
                meta["reason"] = "missing Adobe account password"
                meta["updated_at"] = now
                continue
            status = str(meta.get("status") or "available")
            if status == "in_use" and now - int(meta.get("leased_at") or 0) > stale_seconds:
                status = "available"
                meta["status"] = "available"
                meta["reason"] = "stale lease reset"
            if status in skip_statuses or status == "in_use":
                continue
            meta["status"] = "in_use"
            meta["reason"] = reason
            meta["leased_at"] = now
            meta["updated_at"] = now
            picked.append(record)
            state["cursor"] = idx + 1
            if len(picked) >= count:
                break

        save_state(state)
        return picked


def mark_account(email, status, reason=""):
    key = str(email or "").strip().lower()
    if not key:
        return
    with _LOCK:
        state = load_state()
        meta = state.setdefault("emails", {}).setdefault(key, {})
        meta["status"] = status
        meta["reason"] = str(reason or "")[:500]
        meta["updated_at"] = _now()
        if status != "in_use":
            meta.pop("leased_at", None)
        save_state(state)


def reset_status(statuses=None):
    statuses = set(statuses or [])
    with _LOCK:
        state = load_state()
        changed = 0
        for meta in state.setdefault("emails", {}).values():
            current = str(meta.get("status") or "available")
            if statuses and current not in statuses:
                continue
            meta["status"] = "available"
            meta["reason"] = "reset by user"
            meta["updated_at"] = _now()
            meta.pop("leased_at", None)
            changed += 1
        save_state(state)
        return changed
