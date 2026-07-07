# -*- coding: utf-8 -*-
"""从本地 cookie 池(firefly_adobe2api_cookies.json)导出 adobe2api 格式 JSON 卖(多个号导到【一个】文件)。
格式严格按 adobe2api:{exported_at, total, items:[{id, name, cookie}]}。导出的 email 记进 exported_accounts.txt
(标记已导出、下次跳过),号本身保留在本地池继续监控、不删。
用法:
  python _export_a2a.py             # 全部未导出 → 一个文件
  python _export_a2a.py --limit 50  # 只导前 50 个 → 一个文件
  python _export_a2a.py --re-export # 连已导过的也重导
也被 app.py import 调用:sell_stats() / do_export(limit, re_export) / load_exported()
"""
import json, time, os, uuid, argparse
import firefly_register_yescaptcha as fry

BASE = os.path.dirname(os.path.abspath(__file__))
EXPORTED_FILE = os.path.join(BASE, "exported_accounts.txt")
SOLD_LEDGER = os.path.join(BASE, "sold_ledger.json")


def load_sold_ledger():
    try:
        return json.load(open(SOLD_LEDGER, encoding="utf-8"))
    except Exception:
        return {}


def record_sold(items, baseline=4000):
    """记已售号台账:卖出日期 + 基线积分(只记首次卖出,防覆盖原始日期)。
    items 可为 [email,...] / [(email, baseline),...] / {email: baseline}。"""
    led = load_sold_ledger()
    today = time.strftime("%Y-%m-%d")
    chg = False
    if isinstance(items, dict):
        pairs = list(items.items())
    else:
        pairs = []
        for it in items:
            if isinstance(it, (list, tuple)):
                pairs.append((it[0], it[1] if len(it) > 1 else baseline))
            else:
                pairs.append((it, baseline))
    for em, bl in pairs:
        k = str(em).strip().lower()
        if k and k not in led:
            led[k] = {"sold_at": today, "baseline": int(bl or baseline)}
            chg = True
    if chg:
        with open(SOLD_LEDGER, "w", encoding="utf-8") as f:
            json.dump(led, f, ensure_ascii=False, indent=2)


def load_exported():
    s = set()
    if os.path.exists(EXPORTED_FILE):
        for l in open(EXPORTED_FILE, encoding="utf-8", errors="replace"):
            e = l.strip().lower()
            if e:
                s.add(e)
    return s


def _remaining(entries, done):
    return sum(1 for e in entries
               if str(e.get("name") or "").strip() and str(e.get("name") or "").strip().lower() not in done)


def sell_stats():
    """账本:本地池有cookie数 / 已导出数 / 可卖数 / 已导出email集合(前端子号控制台标'已售')。"""
    entries = fry._load_adobe2api_cookie_entries()
    have = [e for e in entries if (e.get("cookie") or "")]
    exported = load_exported()
    pend = [e for e in have if str(e.get("name") or "").strip().lower() not in exported]
    return {"pool": len(have), "exported": len(exported), "sellable": len(pend),
            "exported_emails": sorted(exported)}


def do_export(limit=0, re_export=False, min_total=1000, workers=16):
    """导出本地池【企业号】→ adobe2api json。★门禁:导出前并发查积分,只放行 total>=min_total
    (真团队≈4000);普号(积分<阈值/查不出/personal)一律不导——卖给客户必须是带企业积分的号。
    返回 (fname, out_dict, remain, skipped_personal)。无可导时 fname=None、items=[]。"""
    entries = fry._load_adobe2api_cookie_entries()
    exported = load_exported()
    cands = []
    for e in entries:
        em = str(e.get("name") or "").strip()
        ck = e.get("cookie") or ""
        if not em or not ck:
            continue
        if not re_export and em.lower() in exported:
            continue
        cands.append((em, ck))
    if limit:
        cands = cands[:limit]
    # —— 企业门禁:并发查积分,只放行 total>=min_total,普号拦下不导 ——
    import _quota
    import concurrent.futures as cf
    pending = []      # (em, ck, total)
    personal = []     # 被拦的普号 email(积分<阈值/查不出)
    if cands:
        def _chk(item):
            em, ck = item
            try:
                q = _quota.query_quota(ck)
            except Exception:
                q = {}
            return em, ck, (q.get("total") or 0)
        with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for em, ck, tot in ex.map(_chk, cands):
                (pending if tot >= min_total else personal).append(
                    (em, ck, tot) if tot >= min_total else em)
    if not pending:
        return None, {"exported_at": int(time.time()), "total": 0, "items": []}, _remaining(entries, exported), len(personal)
    out = {"exported_at": int(time.time()), "total": len(pending),
           "items": [{"id": uuid.uuid4().hex[:8], "name": em, "cookie": ck} for em, ck, _t in pending]}
    fname = "refresh-cookies-export-%s.json" % time.strftime("%Y%m%d-%H%M%S")
    with open(os.path.join(BASE, fname), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    with open(EXPORTED_FILE, "a", encoding="utf-8") as f:
        for em, _ck, _t in pending:
            f.write(em.lower() + "\n")
    record_sold([(em, tot) for em, _ck, tot in pending])   # ★记卖出日期+卖时基线积分
    done = exported | {em.lower() for em, _, _ in pending}
    return fname, out, _remaining(entries, done), len(personal)


def ledger_view(with_current=False, workers=16):
    """已售追踪:按卖出日期分组,每号给 卖时基线 + (可选)当前积分 + 已用 + 是否未用。
    with_current=True 才并发查当前积分(慢),否则只返回台账里的日期/基线(快)。"""
    led = load_sold_ledger()
    items = sorted(led.items(), key=lambda kv: (kv[1].get("sold_at") or "", kv[0]), reverse=True)
    currents = {}
    if with_current and items:
        import _quota
        import concurrent.futures as cf
        ckmap = {str(e.get("name") or "").lower(): (e.get("cookie") or "")
                 for e in fry._load_adobe2api_cookie_entries()}

        def _cur(em):
            ck = ckmap.get(em.lower(), "")
            if not ck:
                return None
            try:
                return _quota.query_quota(ck).get("available")
            except Exception:
                return None
        ems = [k for k, _ in items]
        with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for em, av in zip(ems, ex.map(_cur, ems)):
                currents[em] = av
    by_date = {}
    for em, info in items:
        bl = int(info.get("baseline") or 0)
        now = currents.get(em)
        used = (bl - now) if (with_current and isinstance(now, int)) else None
        by_date.setdefault(info.get("sold_at") or "?", []).append({
            "email": em, "sold_at": info.get("sold_at"), "baseline": bl,
            "current": now if with_current else None, "used": used,
            "unused": (used is not None and used <= 1),
        })
    groups = [{"date": d, "count": len(rs), "rows": rs}
              for d, rs in sorted(by_date.items(), reverse=True)]
    return {"total_sold": len(led), "groups": groups, "with_current": with_current}


def backfill_ledger(sold_at="历史", baseline=4000):
    """把【已导出但不在台账】的历史已售号回填进 sold_ledger(日期标"历史"、基线按4000估)。
    目的:历史号没记基线,但回填后可靠"当前积分"看出谁满额没用(白占)。返回 {added, total_sold}。"""
    led = load_sold_ledger()
    exported = load_exported()
    n = 0
    for em in exported:
        k = str(em).strip().lower()
        if k and k not in led:
            led[k] = {"sold_at": sold_at, "baseline": int(baseline), "backfilled": True}
            n += 1
    if n:
        with open(SOLD_LEDGER, "w", encoding="utf-8") as f:
            json.dump(led, f, ensure_ascii=False, indent=2)
    return {"added": n, "total_sold": len(led)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只导前 N 个(0=全部未导出),都导到一个文件")
    ap.add_argument("--re-export", action="store_true", help="连已导过的也重导")
    ap.add_argument("--min-total", type=int, default=1000, help="积分(total)≥此值才导出卖(普号被拦)")
    args = ap.parse_args()
    fname, out, remain, skipped = do_export(limit=args.limit, re_export=args.re_export, min_total=args.min_total)
    if not out["items"]:
        print("没有可导出的企业号(普号已拦 %d 个;本地池空/都导过了——加 --re-export 可重导)" % skipped, flush=True)
        return 1
    print("✅ 导出 %d 个企业号 → %s (一个文件)" % (out["total"], fname), flush=True)
    if skipped:
        print("   ⚠ 已拦下 %d 个普号(无企业积分,不卖给客户)" % skipped, flush=True)
    print("   已标记导出、记入已售台账(日期+基线);本地池还剩未导出 %d 个" % remain, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
