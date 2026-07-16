# -*- coding: utf-8 -*-
"""3P 探测打标(后台):对本地池每个有 cookie 的子号探一次 3P 模型(默认 nano-banana,走 cost 预检、
不实际生成、不扣积分),把【探不通=不可用】的号自动从本地池(firefly_adobe2api_cookies.json)删除,
这样导出/推送/换号所有出口一处删、全干净,只让能用的号进生产+导出。

判定:
  - 探到 paid / free  = ✅可用(任一次成功立即放行,避免网络抖动误删好号)
  - 408 / 403无权益 / 401 / 超时 / query_error = ⚠不可用(连探3次都不通才判废、删号)
被删的号记进 probe_3p_removed.json 备查(谁/什么状态/几点删);可用号打标存 probe_3p.json。

用法: python _probe3p.py [--workers 12] [--dry-run]   # --dry-run 只探不删,先看会删哪些
"""
import sys, os, json, time
import concurrent.futures as cf
from collections import Counter

import _quota
import firefly_register_yescaptcha as fry
import network_proxy

BASE = os.path.dirname(os.path.abspath(__file__))
PROBE_FILE = os.path.join(BASE, "probe_3p.json")            # 最近一次探测:可用号打标
REMOVED_FILE = os.path.join(BASE, "probe_3p_removed.json")  # 被删废号累积记录(备查)

# 判据:veo3.1 为主 + veo-fast 兜底。这俩 cost 预检对企业号稳定返回 paid,能真实反映账号 3P
# 通道是否可用(408/无权益会暴露)。nano-banana/kling 的 cost 预检 metadata 复杂、对所有号都
# 返回 E400 无法区分好坏,故不用作判据(不影响这些模型实际生成时能否用)。
_PROBE_FEATURES = [
    ("veo3.1", "firefly_3p:external:veo_3", {"videoDuration": "5.0"}),
    ("veo-fast", "firefly_3p:external:veo_3_fast", {"videoDuration": "5.0"}),
]
_FEAT_LABEL = "veo3.1(+veo-fast兜底)"
_USABLE = ("paid", "free")


def probe_one(cookie, proxy):
    """刷 token → 探 3P(veo3.1 主、veo-fast 兜底,cost 预检不扣积分)。返回 (state, usable)。
    任一 feature 任一次 paid/free → 立即判可用(避免抖动误删好号);全部探不通才判废。"""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    tok, note = None, ""
    for _ in range(2):                       # 刷 token 最多2次(防瞬时网络错)
        tok, note = _quota._refresh_to_token(cookie, proxies)
        if tok:
            break
        time.sleep(0.8)
    if not tok:
        return ("cookie_dead:%s" % str(note)[:24], False)   # cookie 本身死了也算不可用
    last = "unknown"
    for label, feat, md in _PROBE_FEATURES:
        for _ in range(2):                   # 每个 feature 探最多2次
            st = _quota._bks_state(tok, feat, md, proxies, retries=1)
            if st in _USABLE:
                return ("%s:%s" % (label, st), True)   # 明确可用,立即放行
            last = "%s:%s" % (label, st)
            if st in ("no_entitlement", "auth_failed"):
                break                        # 明确无权益/认证失败:这个feature别重试,换下一个
            time.sleep(0.8)                  # 408/超时/error:抖动可能,重试确认
    return (last, False)                     # 所有 feature 都不通 → 判废


def _scan_tasks():
    """从 console_children 取 (console, email, cookie),只返回有cookie能探的(换号要知道母号)。"""
    cc = json.load(open(os.path.join(BASE, "console_children.json"), encoding="utf-8")).get("consoles", {})
    ckmap = {str(e.get("name") or "").lower(): (e.get("cookie") or "") for e in fry._load_adobe2api_cookie_entries()}
    tasks = []
    for con, kids in cc.items():
        for c in (kids or []):
            em = str(c.get("email") or "").strip()
            ck = ckmap.get(em.lower(), "")
            if em and ck:
                tasks.append((con, em, ck))
    return tasks


def _swap_dead(dead, dry_run, export_delay, swap_workers, emit):
    """换号:不可用号按母号分组,调 admin_jil_swap 踢旧+加新+导cookie进池(--no-push 不推adobe)。"""
    import subprocess
    import tempfile
    byc = {}
    no_con = []
    for con, em, _st in dead:
        if con:
            byc.setdefault(con, []).append(em)
        else:
            no_con.append(em)
    if no_con:
        emit("   ⚠ %d 个不可用号在清单里找不到母号、跳过: %s" % (
            len(no_con), ", ".join(no_con[:5]) + ("…" if len(no_con) > 5 else "")), flush=True)
    if not byc:
        emit("   没有可换的号(不可用号都没母号归属)", flush=True)
        return 0
    swaps = [{"console": con, "old": olds} for con, olds in byc.items()]
    total = sum(len(s["old"]) for s in swaps)
    emit("#### 探测换号:%d 个不可用子号 / %d 个母号 → admin_jil_swap%s ####" % (
        total, len(swaps), " (DRY-RUN只看不换)" if dry_run else " (踢旧+加新+导cookie进池,不推adobe)"), flush=True)
    fd, path = tempfile.mkstemp(prefix="probe_swap_", suffix=".json", dir=BASE)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(swaps, f, ensure_ascii=False)
    cmd = [sys.executable, os.path.join(BASE, "admin_jil_swap.py"), "--swaps-file", path,
           "--workers", str(swap_workers), "--export-delay", str(export_delay)]
    cmd += ["--dry-run"] if dry_run else ["--then-extract", "--no-push", "--throttle"]   # ★限速错峰防风控
    emit("#### 调用: admin_jil_swap %s ####" % " ".join(cmd[2:]), flush=True)
    return subprocess.call(cmd)


def _delete_dead(dead, emit):
    """删除(备选):不可用号从本地池删 + 已售清单清掉 + 记 removed 备查。"""
    deadset = {em.lower() for _con, em, _st in dead}
    entries = fry._load_adobe2api_cookie_entries()
    kept = [e for e in entries if str(e.get("name") or "").strip().lower() not in deadset]
    fry._write_adobe2api_cookie_entries(kept)
    try:
        import _export_a2a
        cur = _export_a2a.load_exported()
        b4 = len(cur)
        cur -= deadset
        if b4 - len(cur):
            with open(_export_a2a.EXPORTED_FILE, "w", encoding="utf-8") as f:
                for e in sorted(cur):
                    f.write(e + "\n")
            emit("   已售清单同步清掉 %d 个(号位变回未售)" % (b4 - len(cur)), flush=True)
    except Exception as xe:
        emit("清理已售清单异常: %s" % str(xe)[:60], flush=True)
    removed_log = {}
    if os.path.exists(REMOVED_FILE):
        try:
            removed_log = json.load(open(REMOVED_FILE, encoding="utf-8"))
        except Exception:
            removed_log = {}
    today = time.strftime("%Y-%m-%d %H:%M")
    for _con, em, st in dead:
        removed_log[em.lower()] = {"state": st, "removed_at": today}
    with open(REMOVED_FILE, "w", encoding="utf-8") as f:
        json.dump(removed_log, f, ensure_ascii=False, indent=2)
    emit("   已从本地池删除 %d 个不可用号" % len(dead), flush=True)


def run(workers=12, dry_run=False, action="swap", export_delay=0, swap_workers=3, emit=print):
    proxy = network_proxy.configured_proxy() or None
    tasks = _scan_tasks()
    act_label = "换号" if action == "swap" else "清号"
    emit("==== 3P探测%s:%d 个有cookie子号,探 %s(cost预检·不扣积分),并发%d,走%s%s ====" % (
        act_label, len(tasks), _FEAT_LABEL, workers, "代理" if proxy else "直连", " [DRY-RUN]" if dry_run else ""), flush=True)
    if not tasks:
        emit("没有可探的号(清单里有cookie的为0)", flush=True)
        return 0
    done = [0]
    n = len(tasks)

    def _one(item):
        con, em, ck = item
        st, ok = probe_one(ck, proxy)
        done[0] += 1
        emit("[%d/%d] %-42s %-22s %s" % (done[0], n, em, st, "✅可用" if ok else ("⚠不可用·" + act_label)), flush=True)
        return con, em, st, ok

    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        results = list(ex.map(_one, tasks))

    usable = [(em, st) for _con, em, st, ok in results if ok]
    dead = [(con, em, st) for con, em, st, ok in results if not ok]

    # 写打标(给前端逐行标 🧪可用/🧪不可用)
    probe = {"checked_at": int(time.time()), "feature": _FEAT_LABEL,
             "usable": {em.lower(): st for em, st in usable},
             "removed_now": {em.lower(): st for _con, em, st in dead},
             "dry_run": bool(dry_run), "action": action}
    with open(PROBE_FILE, "w", encoding="utf-8") as f:
        json.dump(probe, f, ensure_ascii=False, indent=2)

    dist = Counter(st for _con, _em, st, _ok in results)
    emit("\n==== 探完:可用 %d | 不可用 %d ====" % (len(usable), len(dead)), flush=True)
    emit("   状态分布: " + ", ".join("%s=%d" % (k, v) for k, v in dist.most_common()), flush=True)
    if not dead:
        emit("没有不可用号,无需处理", flush=True)
        return 0
    if action == "swap":
        return _swap_dead(dead, dry_run, export_delay, swap_workers, emit)
    if not dry_run:
        _delete_dead(dead, emit)
    else:
        emit("   (DRY-RUN未删)", flush=True)
    return 0


def stats():
    """给前端:最近探测结果摘要 + 累计被删数。"""
    probe = {}
    removed = {}
    try:
        probe = json.load(open(PROBE_FILE, encoding="utf-8"))
    except Exception:
        probe = {}
    try:
        removed = json.load(open(REMOVED_FILE, encoding="utf-8"))
    except Exception:
        removed = {}
    usable = probe.get("usable") or {}
    removed_now = probe.get("removed_now") or {}
    marks = {}                                   # 每号探测状态 → 前端在子号列表逐行标 🧪可用/🧪将删
    for em, st in usable.items():
        marks[em] = {"ok": True, "state": st}
    for em, st in removed_now.items():
        marks[em] = {"ok": False, "state": st}
    return {"checked_at": probe.get("checked_at"), "feature": probe.get("feature"),
            "usable_count": len(usable),
            "removed_now": len(removed_now),
            "dry_run": probe.get("dry_run", False),
            "removed_total": len(removed),
            "usable_emails": sorted(usable.keys()),
            "marks": marks}


def main():
    import argparse
    ap = argparse.ArgumentParser(description="3P探测:不可用号换号(默认)或删池")
    ap.add_argument("--workers", type=int, default=12, help="探测并发")
    ap.add_argument("--dry-run", action="store_true", help="只探不动,先看会处理哪些")
    ap.add_argument("--action", choices=["swap", "delete"], default="swap", help="swap=不可用号换号(默认);delete=从本地池删")
    ap.add_argument("--export-delay", type=int, default=0, help="换号后等N秒导出(等权益传播;默认0=不干等,靠每号普号重试兜底)")
    ap.add_argument("--swap-workers", type=int, default=3, help="换号子流程并发")
    a = ap.parse_args()
    return run(workers=a.workers, dry_run=a.dry_run, action=a.action,
               export_delay=a.export_delay, swap_workers=a.swap_workers)


if __name__ == "__main__":
    sys.exit(main())
