# -*- coding: utf-8 -*-
"""桥接：复用产号系统(FF_dist)的 IP 节点池 _ippool —— 每号轮询一个去重出口IP的本地代理端口，
防 Adobe per-IP 软封(incorrect password / account not found 假报)。共享同一个 mihomo 核与节点池，不重复占资源。
路径可用环境变量 FF_DIST_DIR 覆盖。"""
import os
import sys
import threading

FF_DIST = os.environ.get("FF_DIST_DIR", r"C:\Users\Administrator\Desktop\FF_dist")
_lock = threading.Lock()
_mod = None
_core_checked = [False]


def _ipp():
    global _mod
    if _mod is None:
        with _lock:
            if _mod is None:
                if os.path.isdir(FF_DIST) and FF_DIST not in sys.path:
                    sys.path.insert(0, FF_DIST)
                import _ippool  # 产号那套的 IP 池核心
                _mod = _ippool
    return _mod


def available():
    """池模块可加载且有健康节点(已测出口IP去重) → True。"""
    try:
        return len(_ipp().healthy_nodes()) > 0
    except Exception:
        return False


def ensure_core():
    """mihomo 内核没在跑就启动一次(产号在跑则直接复用)。"""
    try:
        p = _ipp()
        if not p.core_running():
            print("[IP池] mihomo 内核未运行，正在启动…", flush=True)
            cnt, _pid = p.start_core()
            print(f"[IP池] 已启动 {cnt} 个端口", flush=True)
        _core_checked[0] = True
        return True
    except Exception as exc:
        print(f"[IP池] 启动内核失败: {str(exc)[:80]}", flush=True)
        return False


def pick_proxy():
    """轮询取一个干净出口IP的本地代理 http://127.0.0.1:port；不可用返回 None。"""
    try:
        p = _ipp()
        if not _core_checked[0]:
            ensure_core()
        r = p.next_proxy()
        if r:
            return r.get("proxy")
    except Exception as exc:
        print(f"[IP池] 取代理失败: {str(exc)[:80]}", flush=True)
    return None


def status():
    try:
        p = _ipp()
        healthy = p.healthy_nodes()
        return {"ok": True, "core_running": bool(p.core_running()), "healthy": len(healthy)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:120]}


# ---------- 节点池管理(供团队工具 IP池面板;操作的是与产号共享的同一个池) ----------
def _cc_of(n):
    p = _ipp()
    try:
        return (n.get("exit_cc") or "").upper() or p._name_cc(n.get("name", ""))
    except Exception:
        return (n.get("exit_cc") or "").upper()


def nodes_view(allow_cc=None):
    """全部节点 + 国家/健康判定。allow_cc 给定时,只有国家在内的健康节点算 healthy。"""
    p = _ipp()
    allow = set([str(c).strip().upper() for c in (allow_cc or []) if str(c).strip()]) or None
    out = []
    for n in p.list_nodes():
        cc = _cc_of(n)
        try:
            fake = bool(p._is_fake(n.get("name", "")))
        except Exception:
            fake = False
        base_ok = bool(n.get("enabled") and n.get("exit_ip") and not n.get("dup")
                       and n.get("delay") is not None and not fake)
        in_allow = (allow is None) or (cc in allow)
        out.append({
            "name": n.get("name"), "cc": cc, "exit_ip": n.get("exit_ip"),
            "delay": n.get("delay"), "enabled": bool(n.get("enabled")),
            "dup": bool(n.get("dup")), "fake": fake, "src": n.get("src", ""),
            "healthy": base_ok and in_allow,
        })
    return out


def country_counts():
    """每国可用(健康,不含国家筛选)节点数,用于国家多选清单。"""
    cnt = {}
    for n in nodes_view():
        if not (n["enabled"] and n["exit_ip"] and not n["dup"] and not n["fake"] and n["delay"] is not None):
            continue
        cc = n["cc"] or "??"
        cnt[cc] = cnt.get(cc, 0) + 1
    return cnt


def add_subscription(url):
    return _ipp().add_subscription(url)   # (新增数, 总数)


def add_nodes_text(text):
    return _ipp().add_nodes_text(text)


def remove_node(name):
    return _ipp().remove_node(name)


def set_enabled(name, en):
    _ipp().set_enabled(name, en)


def start_core():
    return _ipp().start_core()


def stop_core():
    _ipp().stop_core()
