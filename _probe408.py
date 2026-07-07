# -*- coding: utf-8 -*-
"""真实 408 探测(只检测·不动号):对本地池每个有 cookie 的子号,真实打
firefly-3p generate-async(nano-banana),看【实际状态码】判:
  200 = ✅能出图   403 user_not_entitled = org无3P权益   408 colligo甩 = org被风控甩负载
跟 _probe3p(cost 预检)不同——这是真实提交,反映 Adobe 网关到底给不给生成。
判据来自记忆 [[firefly-3p-408-root-cause]]:408 看 x-colligo-timeout、403 看 x-access-error。
用法: python _probe408.py [--workers 5]
"""
import sys, os, json, time, hashlib, base64, argparse
import concurrent.futures as cf
from collections import Counter
import importlib.util as _ilu

import _quota
import network_proxy
import _probe3p   # 复用 _scan_tasks(取 console/email/cookie)
from curl_cffi import requests as creq

BASE = os.path.dirname(os.path.abspath(__file__))
PROBE_FILE = os.path.join(BASE, "probe_408.json")
SUBMIT = "https://firefly-3p.ff.adobe.io/v2/3p-images/generate-async"
APIK = "clio-playground-web"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
PROMPT = "a small red apple on a white table"
TRIES = 5   # 每号 generate-async 重试次数(408多为网关瞬时过载,重试恢复;和adobe2api体检一致)

# adobe2api 的 payload 构造器(单文件加载,payloads.py 只 import time/typing,无 fastapi 依赖)
_PAYLOAD_PATHS = [
    r"E:\adobe2api-master\core\models\payloads.py",
    r"E:\adobe2api-master-backup-before-update-20260530124631\core\models\payloads.py",
    os.path.join(BASE, "payloads.py"),
]
bipc = None
for _p in _PAYLOAD_PATHS:
    if os.path.exists(_p):
        try:
            _spec = _ilu.spec_from_file_location("a2a_payloads_408", _p)
            _m = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_m)
            bipc = _m.build_image_payload_candidates
            break
        except Exception:
            pass


def _uid(t):
    try:
        p = t.split(".")[1]; p += "=" * (-len(p) % 4)
        return str(json.loads(base64.urlsafe_b64decode(p)).get("user_id") or "")
    except Exception:
        return ""


def probe_one(cookie, proxy):
    """cookie → firefly token → 真实 generate-async 提交 → 分类。返回 (state, usable_bool)。"""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    tok = None
    for _ in range(2):
        tok, _note = _quota._refresh_to_token(cookie, proxies)
        if tok:
            break
        time.sleep(0.6)
    if not tok:
        return ("cookie_dead", False)
    if not bipc:
        return ("payload构造器没找到(查adobe2api路径)", False)
    try:
        pls = bipc(prompt=PROMPT, aspect_ratio="1:1", output_resolution="1K",
                   upstream_model_id="gemini-flash", upstream_model_version="nano-banana-2")
    except Exception as e:
        return ("payload失败:%s" % str(e)[:24], False)
    uid = _uid(tok)
    nonce = hashlib.sha256(("%s-%s" % (uid, PROMPT)).encode()).hexdigest() if uid else ""
    h = {"Authorization": "Bearer " + tok, "x-api-key": APIK, "content-type": "application/json", "accept": "*/*",
         "user-agent": UA, "origin": "https://firefly.adobe.com", "referer": "https://firefly.adobe.com/"}
    if nonce:
        h["x-nonce"] = nonce
    # ★重试:408 多为 Adobe 网关瞬时过载(并发30-45%),重试能恢复200——和 adobe2api 体检一个口径,
    #   不是真死;只有重试 N 次仍 408 才判"持续408"。任意一次 200 立即放行。
    last_code = None; last_xct = ""
    for i in range(TRIES):
        try:
            r = creq.post(SUBMIT, headers=h, json=pls[0], impersonate="chrome124", timeout=60, proxies=proxies, verify=False)
        except Exception as e:
            last_code = "submit异常:%s" % str(e)[:16]; time.sleep(1.0); continue
        code = r.status_code
        if code == 200:
            return ("200可出图(第%d次)" % (i + 1), True)
        xct = r.headers.get("x-colligo-timeout", ""); xae = r.headers.get("x-access-error", "")
        if code == 403:
            return ("403无权益:%s" % (xae or "user_not_entitled"), False)   # 权益问题,重试无用
        if code == 422:
            return ("422档不对(非企业4000档)", False)
        last_code = code; last_xct = xct
        if code == 408:
            time.sleep(1.0 + i * 0.7); continue        # 瞬时过载,退避重试
        time.sleep(0.8)
    if last_code == 408:
        return ("持续408(重试%d次仍被甩 colligo=%s)" % (TRIES, last_xct or "0.0"), False)
    return ("%s持续失败" % last_code, False)


def run(workers=5, emit=print):
    tasks = _probe3p._scan_tasks()
    emit("==== 真实408探测:%d 个有cookie子号,真实打 generate-async(nano),每号独立住宅IP,并发%d ====" % (len(tasks), workers), flush=True)
    emit("   (★这是真实提交:能出图的号会排1张图/消耗1次额度;408/403不消耗)", flush=True)
    if not tasks:
        emit("没有可探的号(清单里有cookie的为0)", flush=True)
        return 0
    done = [0]; n = len(tasks)

    def _one(item):
        con, em, ck = item
        px = network_proxy.proxy_for_id(em) or None   # 每子号自己的固定住宅IP
        st, ok = probe_one(ck, px)
        done[0] += 1
        tag = "✅200能出图" if ok else ("❌" + st if ("408" in st or "403" in st) else "⚠" + st)
        emit("[%d/%d] %-40s %s" % (done[0], n, em[:40], tag), flush=True)
        return con, em, st, ok

    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        results = list(ex.map(_one, tasks))

    ok200 = [(em, st) for _c, em, st, k in results if k]
    bad = [(em, st) for _c, em, st, k in results if not k]
    n408 = sum(1 for _c, _e, st, k in results if (not k) and "408" in st)
    n403 = sum(1 for _c, _e, st, k in results if (not k) and "403" in st)
    probe = {"checked_at": int(time.time()),
             "ok": {em.lower(): st for em, st in ok200},
             "bad": {em.lower(): st for em, st in bad},
             "n200": len(ok200), "n408": n408, "n403": n403, "total": n}
    with open(PROBE_FILE, "w", encoding="utf-8") as f:
        json.dump(probe, f, ensure_ascii=False, indent=2)

    dist = Counter(st.split(":")[0] for _c, _e, st, _k in results)
    emit("\n==== 真实408探完:✅200能出图 %d | ❌408被甩 %d | ❌403无权益 %d | ⚠其它 %d ====" % (
        len(ok200), n408, n403, len(bad) - n408 - n403), flush=True)
    emit("   状态分布: " + ", ".join("%s=%d" % (k, v) for k, v in dist.most_common()), flush=True)
    emit("   结论:200多=号能用;大面积408=org级风控被甩;大面积403=org无3P权益(见记忆firefly-3p-408-root-cause)", flush=True)
    return 0


def stats():
    """给前端:最近一次真实408探测结果 + 每号打标(🎯200/🎯408/🎯403)。"""
    probe = {}
    try:
        probe = json.load(open(PROBE_FILE, encoding="utf-8"))
    except Exception:
        probe = {}
    marks = {}
    for em, st in (probe.get("ok") or {}).items():
        marks[em] = {"ok": True, "state": st}
    for em, st in (probe.get("bad") or {}).items():
        marks[em] = {"ok": False, "state": st}
    return {"checked_at": probe.get("checked_at", 0), "marks": marks,
            "n200": probe.get("n200", 0), "n408": probe.get("n408", 0),
            "n403": probe.get("n403", 0), "total": probe.get("total", 0)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=5)
    a = ap.parse_args()
    sys.exit(run(workers=a.workers))
