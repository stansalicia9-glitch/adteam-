# -*- coding: utf-8 -*-
"""协议版批量导出子号 cookie(零浏览器),替代 firefly_login_extract_cookies.py(浏览器版)。
读 accounts 文件(console_children raw) → sub_login_cookie 纯协议导 cookie → merge 进 firefly_adobe2api_cookies.json。
推送由调用方(app.py 的 after_extract → cookie_push.push_console_async)负责,本脚本只写 cookie 文件。
接口对齐浏览器版:--accounts/--workers/--proxy;浏览器版才有的 --headless/--ip-pool/--retry-rounds 等被忽略。"""
import sys, os, argparse, re, time, random
import concurrent.futures as cf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
import admin_login_protocol as alp
import firefly_register_yescaptcha as fry   # 复用其 cookie 文件读写(格式与浏览器版一致)
import network_proxy
try:
    import _quota
except Exception:
    _quota = None


def _parse_account(raw):
    raw = str(raw or "").strip()
    if not raw:
        return None
    segs = re.split(r"----|\s+", raw)
    email = (segs[0] if segs else "").strip()
    if not email or "@" not in email:
        return None
    pw = segs[1].strip() if len(segs) > 1 else ""
    rt = next((s for s in segs[2:] if s.startswith("M.")), "")
    cid = next((s for s in segs[2:] if len(s) == 36 and s.count("-") == 4), "")
    return {"email": email, "password": pw, "refresh_token": rt, "client_id": cid}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts", required=True)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--proxy", default="")
    args, _ignored = ap.parse_known_args()  # 忽略浏览器版独有参数

    base_proxy = (args.proxy or "").strip() or None              # 显式 --proxy 优先(给就全用它)
    use_resi = bool(network_proxy._residential_tpl()) and not base_proxy  # 配了住宅模板且没显式proxy → 每子号不同住宅IP
    # ★限并发≤4:防"一个 org 几百成员短时间同 IP 批量登录"被当账号农场
    workers = max(1, min(int(args.workers or 3), 4))
    accts = []
    with open(args.accounts, encoding="utf-8-sig") as f:
        for line in f:
            a = _parse_account(line)
            if a:
                accts.append(a)
    if not accts:
        print("[协议导出] accounts 文件没有可解析的账号", flush=True)
        return 1
    _mode = "每子号不同住宅IP·限频" if use_resi else ("走代理" if base_proxy else "直连")
    print(f"[协议导出] 共 {len(accts)} 个子号,纯协议导 cookie(并发{workers},零浏览器,{_mode})", flush=True)

    def _one(a):
        em = a["email"]
        if not a.get("password"):
            print(f"  [{em}] ❌ 没密码,跳过", flush=True)
            return em, ""
        time.sleep(random.uniform(0.4, 2.2))                     # ★错峰:别一秒一堆同时登录,分散登录时间(拟人)
        pxy = network_proxy.proxy_for_id(em) if use_resi else (base_proxy or network_proxy.configured_proxy() or None)
        try:
            ck = alp.sub_login_cookie(a, proxy=pxy)
        except Exception as exc:
            print(f"  [{em}] ❌ 导出异常: {str(exc)[:70]}", flush=True)
            return em, ""
        if not ck:
            print(f"  [{em}] ❌ 登录失败/没拿到 cookie", flush=True)
            return em, ""
        tot = "?"
        if _quota:
            try:
                q = _quota.query_quota(ck)
                tot = q.get("total")
            except Exception:
                pass
        print(f"  [{em}] ✅ cookie {len(ck)}字 积分{tot}", flush=True)
        return em, ck

    results = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for em, ck in ex.map(_one, accts):
            if ck:
                results[em.lower()] = (em, ck)

    # merge 进 firefly_adobe2api_cookies.json(单进程主线程一次性写,无并发竞争)
    entries = fry._load_adobe2api_cookie_entries()  # [{name, cookie}]
    by_name = {}
    for e in entries:
        nm = str(e.get("name") or "").strip().lower()
        if nm:
            by_name[nm] = e
    for low, (em, ck) in results.items():
        by_name[low] = {"name": em, "cookie": ck}
    fry._write_adobe2api_cookie_entries(list(by_name.values()))
    print(f"[协议导出] 完成:成功 {len(results)}/{len(accts)},已写 firefly_adobe2api_cookies.json(库内共{len(by_name)}条)", flush=True)
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
