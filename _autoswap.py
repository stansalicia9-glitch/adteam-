# -*- coding: utf-8 -*-
"""全自动换低分号:扫所有子号额度 → 找 <阈值(默认100)分 → 自动删旧+加新+导出+只推新号(调 admin_jil_swap)。
用法: python _autoswap.py [--threshold 100] [--limit N] [--dry-run] [--workers 3] [--export-delay 90]
死号(cookie失效)不在这里换(token查不出额度);这里只换"有效但可用积分<阈值"的。
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import concurrent.futures as cf

import _quota

BASE = os.path.dirname(os.path.abspath(__file__))


def scan_low(threshold, workers=12):
    cc = json.load(open(os.path.join(BASE, "console_children.json"), encoding="utf-8")).get("consoles", {})
    ck = json.load(open(os.path.join(BASE, "firefly_adobe2api_cookies.json"), encoding="utf-8"))
    items = ck.get("items", ck) if isinstance(ck, dict) else ck
    ckmap = {str(i.get("name", "")).strip().lower(): (i.get("cookie") or "") for i in items}
    tasks = []
    for con, kids in cc.items():
        for c in kids:
            em = str(c.get("email", "")).strip().lower()
            cookie = ckmap.get(em, "")
            if cookie:
                tasks.append((con, em, cookie))
    print("扫描 %d 个有CK的子号..." % len(tasks), flush=True)

    def q(t):
        con, em, cookie = t
        r = _quota.query_quota(cookie)
        return (con, em, r.get("token_ok"), r.get("available"))

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(q, tasks))
    low = [(con, em, av) for (con, em, ok, av) in res if ok and (av is not None) and av < threshold]
    low.sort(key=lambda x: x[2])
    return low


def main():
    ap = argparse.ArgumentParser(description="全自动换低分号")
    ap.add_argument("--threshold", type=int, default=100, help="可用积分低于此值的换掉,默认100")
    ap.add_argument("--limit", type=int, default=0, help="只换前N个(0=全部)")
    ap.add_argument("--dry-run", action="store_true", help="只看会换哪些,不实际操作")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--export-delay", type=int, default=180)
    a = ap.parse_args()

    low = scan_low(a.threshold)
    print("=== 低于 %d 分的子号: %d 个 ===" % (a.threshold, len(low)), flush=True)
    for con, em, av in low:
        print("  %-42s 母号=%-38s %s分" % (em, con, av), flush=True)
    if a.limit and len(low) > a.limit:
        low = low[: a.limit]
        print("(本次只处理前 %d 个)" % a.limit, flush=True)
    if not low:
        print("没有低分号,结束", flush=True)
        return 0

    byc = {}
    for con, em, av in low:
        byc.setdefault(con, []).append(em)
    swaps = [{"console": con, "old": olds} for con, olds in byc.items()]
    total = sum(len(s["old"]) for s in swaps)
    print("#### 将换 %d 个子号 / %d 个母号 ####" % (total, len(swaps)), flush=True)

    fd, path = tempfile.mkstemp(prefix="autoswap_", suffix=".json", dir=BASE)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(swaps, f, ensure_ascii=False)

    cmd = [sys.executable, os.path.join(BASE, "admin_jil_swap.py"), "--swaps-file", path,
           "--workers", str(a.workers), "--export-delay", str(a.export_delay)]
    cmd += ["--dry-run"] if a.dry_run else ["--then-extract", "--throttle"]   # ★全自动监控:限速错峰(母号串行+间隔+删加延迟)防风控
    print("#### 调用换号: admin_jil_swap %s ####" % " ".join(cmd[3:]), flush=True)
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
