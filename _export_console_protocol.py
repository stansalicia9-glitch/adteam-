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
    ap.add_argument("--retry-rounds", type=int, default=2, help="失败号重试轮数(等Firefly权益传播/429冷却;别太多轮,反复重试被限流号=火上浇油)")
    ap.add_argument("--retry-wait", type=int, default=180, help="每轮之间等待秒数(429账号级限流要几分钟才冷却,75s太短)")
    args, _ignored = ap.parse_known_args()  # 忽略浏览器版独有参数

    base_proxy = (args.proxy or "").strip() or None              # 显式 --proxy 优先(给就全用它)
    use_resi = bool(network_proxy._residential_tpl()) and not base_proxy  # 配了住宅模板且没显式proxy → 每子号不同住宅IP
    # ★限并发≤3:防"短时间大量登录"触发 Adobe 账号级 429 限流(实测隔离单测必过、并发burst才429)
    workers = max(1, min(int(args.workers or 2), 3))
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
            return a, ""
        time.sleep(random.uniform(1.5, 5.0))                     # ★错峰加大:别一秒一堆同时登录,分散burst防429(429比慢一点更伤)
        pxy = network_proxy.proxy_for_id(em) if use_resi else (base_proxy or network_proxy.configured_proxy() or None)
        try:
            ck = alp.sub_login_cookie(a, proxy=pxy)
        except Exception as exc:
            print(f"  [{em}] ❌ 导出异常: {str(exc)[:70]}", flush=True)
            return a, ""
        if not ck:
            print(f"  [{em}] ❌ 登录失败/没拿到 cookie(权益未传播/限流/需MFA)", flush=True)
            return a, ""
        tot = "?"
        if _quota:
            try:
                q = _quota.query_quota(ck)
                tot = q.get("total")
            except Exception:
                pass
        print(f"  [{em}] ✅ cookie {len(ck)}字 积分{tot}", flush=True)
        return a, ck

    def _flush(results):
        """把目前成功的 cookie merge 进 firefly_adobe2api_cookies.json(主线程调,无并发竞争)。
        ★每轮结束就调一次——中途被停/进程被杀也不丢已成功的号(之前只在全部跑完才写→停一下全丢)。"""
        if not results:
            return 0
        entries = fry._load_adobe2api_cookie_entries()  # [{name, cookie}]
        by_name = {}
        for e in entries:
            nm = str(e.get("name") or "").strip().lower()
            if nm:
                by_name[nm] = e
        for low, (em, ck) in results.items():
            by_name[low] = {"name": em, "cookie": ck}
        fry._write_adobe2api_cookie_entries(list(by_name.values()))
        return len(by_name)

    # ★分轮跑:刚加的子号第一次多半"权益没传播/被门禁拒/429限流",隔一会重试失败号(等传播+冷却),
    #   比一次性判死大幅提升成功率。成功的不再重试。★每轮结束立即落盘,进程被杀不丢已成功的。
    results = {}
    pending = list(accts)
    rounds = max(1, int(args.retry_rounds or 1))
    for rnd in range(rounds):
        if rnd > 0:
            wait = max(0, int(args.retry_wait or 0))
            # ★先把上一轮成功的落盘,再等——万一在这 180s 等待期间被停掉,已成功的不丢
            if results:
                _flush(results)
                print(f"[协议导出] 已落盘 {len(results)} 个成功号(等待期间被停也不丢)", flush=True)
            print(f"[协议导出] 第 {rnd + 1}/{rounds} 轮:{len(pending)} 个失败号等 {wait}s 后重试(等Firefly权益传播/429冷却)…", flush=True)
            time.sleep(wait)
        cur, pending = pending, []
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            for a, ck in ex.map(_one, cur):
                if ck:
                    results[a["email"].lower()] = (a["email"], ck)
                else:
                    pending.append(a)
        _flush(results)   # ★每轮跑完立即落盘
        if not pending:
            break
    if pending:
        print(f"[协议导出] {len(pending)} 个号 {rounds} 轮仍失败:{[a['email'] for a in pending]}"
              f"(多半权益还没传播,再等几分钟单独重导;或该号需MFA/SMS无法纯协议)", flush=True)

    total_in_lib = _flush(results)
    print(f"[协议导出] 完成:成功 {len(results)}/{len(accts)},已写 firefly_adobe2api_cookies.json(库内共{total_in_lib}条)", flush=True)
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
