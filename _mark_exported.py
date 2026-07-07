# -*- coding: utf-8 -*-
"""把一个【已发给客户的】adobe2api 导出 json(name 可能是真实姓名、不是 email)里的号,
通过 account_id 认号(cookie 刷 token 解出的 user_id 是账号【固有】、不随 cookie 变),标记进 exported_accounts.txt,
下次 _export_a2a.py 自动跳过、不会再次导出卖给别的客户(防撞号)。
用法: python _mark_exported.py <导出json路径> [<导出json路径2> ...]
也被 app.py import 调用:do_mark(paths, log=None) → {total, matched, new, unmatched, pool, pool_indexed}
"""
import json, os, sys
import concurrent.futures as cf
import firefly_register_yescaptcha as fry
import _quota

BASE = os.path.dirname(os.path.abspath(__file__))
EXPORTED_FILE = os.path.join(BASE, "exported_accounts.txt")
WORKERS = 8


def _aid(ck):
    if not ck:
        return ""
    try:
        tok, _ = _quota._refresh_to_token(ck, None)  # 直连刷 token(refresh 接口不限地域)
        return _quota._jwt_account_id(tok) if tok else ""
    except Exception:
        return ""


def do_mark(paths, log=None):
    """用 account_id 认号,把这些导出 json 里的号标记进 exported_accounts.txt。
    返回 {total, matched, new, unmatched, pool, pool_indexed}。log 可选 callable(str)。"""
    def _log(m):
        if log:
            try:
                log(m)
            except Exception:
                pass

    pool = fry._load_adobe2api_cookie_entries()
    pool_items = [(str(e.get("name") or "").strip().lower(), e.get("cookie") or "")
                  for e in pool if e.get("cookie")]
    by_cookie = {ck: em for em, ck in pool_items}
    _log(f"本地池 {len(pool_items)} 个,刷 token 解 account_id 建索引(并发{WORKERS})…")
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        pool_aids = list(ex.map(lambda x: _aid(x[1]), pool_items))
    aid_to_email = {}
    for (em, ck), aid in zip(pool_items, pool_aids):
        if aid:
            aid_to_email[aid] = em
    _log(f"本地池建索引: account_id 命中 {len(aid_to_email)}/{len(pool_items)}")

    existing = set()
    if os.path.exists(EXPORTED_FILE):
        for l in open(EXPORTED_FILE, encoding="utf-8", errors="replace"):
            if l.strip():
                existing.add(l.strip().lower())

    matched, total_items, unmatched = [], 0, 0
    for path in paths:
        try:
            exp = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            _log(f"❌ 读不了 {os.path.basename(path)}: {str(e)[:80]}")
            continue
        items = exp.get("items") or []
        total_items += len(items)
        cks = [it.get("cookie") or "" for it in items]
        need_aid = [ck for ck in cks if ck and ck not in by_cookie]  # cookie 精确没中的才刷
        with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
            aids = dict(zip(need_aid, ex.map(_aid, need_aid)))
        m = u = 0
        for ck in cks:
            em = by_cookie.get(ck) or aid_to_email.get(aids.get(ck, ""))
            if em:
                matched.append(em)
                m += 1
            else:
                u += 1
        unmatched += u
        _log(f"{os.path.basename(path)}: {len(items)} 个 → 认出 {m} | 未认出 {u}")

    new = [x for x in dict.fromkeys(matched) if x not in existing]
    with open(EXPORTED_FILE, "a", encoding="utf-8") as f:
        for x in new:
            f.write(x + "\n")
    return {"total": total_items, "matched": len(matched), "new": len(new),
            "unmatched": unmatched, "pool": len(pool_items), "pool_indexed": len(aid_to_email)}


def main():
    if len(sys.argv) < 2:
        print("用法: python _mark_exported.py <导出json路径> [更多...]")
        return 1
    st = do_mark(sys.argv[1:], log=lambda m: print("  " + m, flush=True))
    print(f"\n✅ 共 {st['total']} 个 | 认出 email {st['matched']} | 新标记 {st['new']} | 未认出 {st['unmatched']}", flush=True)
    if st["unmatched"]:
        print("   未认出 = cookie 已失效刷不出 token,或该号不在本地池。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
