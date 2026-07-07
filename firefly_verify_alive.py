# -*- coding: utf-8 -*-
"""验活(纯 API)：调 Adobe 免登录查号接口判断邮箱是否注册，秒级、不开浏览器。
  POST https://auth.services.adobe.com/signin/v2/users/accounts
  Header: x-ims-clientid: clio-playground-web ; Body: {"username":邮箱,"usernameType":"EMAIL"}
  返回 [{...}] = 活号(已注册) / [] = 死号(Adobe无此账号)
死号在池里标 deprecated → 删加子号(acquire)自动跳过 → 只用活号 → 导cookie接近100%。"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import firefly_mail_pool
try:
    import _proxypool  # 复用产号 IP 池：每次查号走不同干净出口IP
except Exception:
    _proxypool = None

API_URL = "https://auth.services.adobe.com/signin/v2/users/accounts"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "x-ims-clientid": "clio-playground-web",
    "Referer": "https://firefly.adobe.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
}


def _check_email_api(email, proxies=None):
    """返回 alive / dead / unknown。unknown=网络/限流失败(保留不删)。"""
    body = {"username": email, "usernameType": "EMAIL"}
    for attempt in range(6):
        try:
            r = requests.post(API_URL, json=body, headers=HEADERS, timeout=20, proxies=proxies)
            if r.status_code == 200:
                try:
                    j = r.json()
                except Exception:
                    return "unknown"
                if isinstance(j, list):
                    return "alive" if len(j) > 0 else "dead"
                return "unknown"
            if r.status_code == 429:  # 限流：退避重试
                time.sleep(3 + attempt * 2)
                continue
            # 其它非200：偶发，重试几次
            time.sleep(1.5)
        except Exception:
            time.sleep(1.5 + attempt)
    return "unknown"


def _check_one(account, idx, total, proxies, ip_pool=False):
    email = str(account.get("email") or "").strip()
    if not email:
        return "unknown"
    use_proxies = proxies
    if ip_pool and _proxypool is not None:
        pp = _proxypool.pick_proxy()
        if pp:
            use_proxies = {"http": pp, "https": pp}
    r = _check_email_api(email, use_proxies)
    if r == "dead":
        print(f"[{idx}/{total}] ☠ 死号(将从池删除): {email}", flush=True)
    elif r == "alive":
        print(f"[{idx}/{total}] ✅ 活号: {email}", flush=True)
    else:
        print(f"[{idx}/{total}] ❓ 未定(网络失败,保留): {email}", flush=True)
    return r


def run(args):
    accounts = firefly_mail_pool.available_accounts(limit=args.limit)
    total = len(accounts)
    if total == 0:
        print("#### 邮箱池没有待验活的可用号 ####", flush=True)
        return 0
    workers = max(1, min(int(args.workers or 1), 20))
    proxies = {"http": args.proxy, "https": args.proxy} if args.proxy else None
    ip_pool = getattr(args, "ip_pool", False)
    if ip_pool and _proxypool is not None:
        _proxypool.ensure_core()
        print("#### 验活走IP池(每号换干净出口IP) ####", flush=True)
    print(f"#### 验活(API)开始: {total} 个号, 并发 {workers} ####", flush=True)
    t0 = time.time()
    alive = dead = unknown = 0
    done = 0
    dead_emails = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        fut_to_acc = {ex.submit(_check_one, acc, i + 1, total, proxies, ip_pool): acc for i, acc in enumerate(accounts)}
        for fut, acc in fut_to_acc.items():
            r = fut.result()
            done += 1
            if r == "alive":
                alive += 1
            elif r == "dead":
                dead += 1
                em = str(acc.get("email") or "").strip()
                if em:
                    dead_emails.append(em)
            else:
                unknown += 1
            if done % 100 == 0:
                print(f"[进度] {done}/{total} 活{alive} 死{dead} 未定{unknown}", flush=True)
    # 死号直接从邮箱池删除（批量一次写）
    if dead_emails:
        try:
            n = firefly_mail_pool.delete_accounts(emails=dead_emails, mode="selected")
            print(f"#### 已从邮箱池删除 {n} 个死号 ####", flush=True)
        except Exception as exc:
            print(f"#### 删除死号失败(死号仍在池里): {exc} ####", flush=True)
    print("#" * 50, flush=True)
    print(f"#### 验活完成: 活 {alive} / 死 {dead}(已从池删除) / 未定 {unknown}(网络失败,保留) ; 耗时 {time.time()-t0:.0f}s ####", flush=True)
    print("#" * 50, flush=True)
    return 0


def main():
    ap = argparse.ArgumentParser(description="验活(API)：剔除邮箱池里 Adobe 上不存在的死号")
    ap.add_argument("--workers", type=int, default=8, help="并发(最多20，纯HTTP可拉高)")
    ap.add_argument("--limit", type=int, default=0, help="只验前 N 个(0=全部)")
    ap.add_argument("--proxy", default="", help="代理(http://ip:port)")
    ap.add_argument("--headless", action="store_true", help="(兼容旧参数，API模式无意义)")
    ap.add_argument("--ip-pool", dest="ip_pool", action="store_true", help="每次查号走产号IP池的干净出口IP")
    args = ap.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
