# -*- coding: utf-8 -*-
"""Adobe Firefly 团队批量 Web 控制台。
整合：邮箱池(未用/已开)管理、管理员登录(播种)+选定后批量删加、Cookie 导出、实时日志。
启动：python app.py  ->  http://127.0.0.1:5005
"""
import os

# ★清掉本机 VPN/clash 的 HTTP_PROXY 环境变量(在任何 requests 之前):
#   FF 出图/接码/推送用的是【代码里指定的住宅代理】(network_proxy),不该走这个系统代理。
#   VPN/TUN 关了但 env 还留着时,bare requests(Graph读验证码/推adobe2api)会走那个死代理→连不上/卡住。
#   清掉让所有未显式指定代理的请求直连;显式住宅代理(Adobe出图/接码)不受影响。子进程继承此清理后的env。
for _pv in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_pv, None)

import re
import sys
import json
import time
import queue
import threading
import subprocess
import collections
import tempfile

from flask import Flask, request, jsonify, Response, render_template

import firefly_mail_pool as pool
import admin_console_manage as acm
import adobe_umapi as umapi
import adobe_jil as jil
import console_children
import cookie_push

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ADMIN_CONFIG = os.path.join(BASE_DIR, "admin_console_config.json")
REGISTERED = os.path.join(BASE_DIR, "registered_accounts.txt")
ADDED = os.path.join(BASE_DIR, "added_accounts.txt")
MISSING = os.path.join(BASE_DIR, "missing_cookie_accounts.txt")
COOKIES = os.path.join(BASE_DIR, "firefly_adobe2api_cookies.json")
CURRENT_EXPORT_DIR = os.path.join(BASE_DIR, "current_child_exports")
os.makedirs(CURRENT_EXPORT_DIR, exist_ok=True)

app = Flask(__name__)


# --------------------------------------------------------------------------- #
# 公网访问认证(HTTP Basic Auth)。panel_auth.json 设了 password 才启用;没设=纯本地
# 模式不拦截(不影响本机 127.0.0.1 使用)。★开 cpolar/公网穿透前【必须】先设密码,
# 否则后台敞开公网 = 母号全没的灾难。
# --------------------------------------------------------------------------- #
PANEL_AUTH_FILE = os.path.join(BASE_DIR, "panel_auth.json")


def _panel_auth():
    try:
        with open(PANEL_AUTH_FILE, encoding="utf-8-sig") as f:
            d = json.load(f)
        return str(d.get("user") or "admin"), str(d.get("password") or "")
    except Exception:
        return "admin", ""


@app.before_request
def _require_panel_auth():
    _u, _pw = _panel_auth()
    if not _pw:
        return  # 没设密码 = 纯本地模式,不拦(本机直连照常用)
    a = request.authorization
    if a and a.username == _u and a.password == _pw:
        return
    return Response("Authentication required", 401,
                    {"WWW-Authenticate": 'Basic realm="FF5006 Panel"'})


# --------------------------------------------------------------------------- #
# 单任务后台执行器 + 日志 SSE
# --------------------------------------------------------------------------- #
class TaskRunner:
    def __init__(self):
        self.lock = threading.Lock()
        self.procs = {}          # tid -> (proc, name)
        self._seq = 0
        self.logs = collections.deque(maxlen=8000)
        self.subs = []

    def _emit(self, line):
        self.logs.append(line)
        for q in list(self.subs):
            try:
                q.put_nowait(line)
            except Exception:
                pass

    def start(self, cmd, name):
        # 多任务：不再拒绝并发，允许多个任务/多个浏览器同时跑
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        try:
            proc = subprocess.Popen(
                cmd, cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, bufsize=1, text=True, encoding="utf-8", errors="replace",
            )
        except Exception as exc:
            return False, f"启动失败：{exc}"
        with self.lock:
            self._seq += 1
            tid = self._seq
            self.procs[tid] = (proc, name)
        threading.Thread(target=self._reader, args=(tid, proc, name), daemon=True).start()
        return True, "已启动"

    def start_with_callback(self, cmd, name, callback=None):
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        try:
            proc = subprocess.Popen(
                cmd, cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, bufsize=1, text=True, encoding="utf-8", errors="replace",
            )
        except Exception as exc:
            return False, f"启动失败：{exc}"
        with self.lock:
            self._seq += 1
            tid = self._seq
            self.procs[tid] = (proc, name)
        threading.Thread(target=self._reader, args=(tid, proc, name, callback), daemon=True).start()
        return True, "已启动"

    def _reader(self, tid, proc, name, callback=None):
        self._emit(f"$ {name}")
        try:
            for line in proc.stdout:
                self._emit(line.rstrip("\n"))
        except Exception as exc:
            self._emit(f"[读取输出异常] {exc}")
        code = proc.wait()
        self._emit(f"==== 任务结束 [{name}] exit={code} ====")
        with self.lock:
            self.procs.pop(tid, None)
        if callback:
            try:
                callback(code)
            except Exception as exc:
                self._emit(f"[任务回调异常] {exc}")

    @property
    def running(self):
        with self.lock:
            return len(self.procs) > 0

    @property
    def count(self):
        with self.lock:
            return len(self.procs)

    @property
    def name(self):
        with self.lock:
            names = [n for (_, n) in self.procs.values()]
        if not names:
            return ""
        if len(names) == 1:
            return names[0]
        return f"{len(names)} 个任务并行: " + " | ".join(names)

    def stop(self):
        with self.lock:
            items = list(self.procs.items())
        if not items:
            return False
        for tid, (proc, name) in items:
            try:
                if os.name == "nt":
                    subprocess.call(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    proc.terminate()
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
        self._emit(f"[已停止 {len(items)} 个任务，已结束进程树]")
        return True

    def clear_logs(self):
        with self.lock:
            self.logs.clear()
        for q in list(self.subs):
            try:
                q.put_nowait("[日志已清空]")
            except Exception:
                pass
        return True

    def subscribe(self):
        q = queue.Queue(maxsize=4000)
        self.subs.append(q)
        # 新连接只补发最近 300 行历史:以前补发全部(最多8000),手机SSE一重连就瞬间灌8000条→反复卡。
        for line in list(self.logs)[-300:]:
            try:
                q.put_nowait(line)
            except Exception:
                break
        return q

    def unsubscribe(self, q):
        try:
            self.subs.remove(q)
        except Exception:
            pass


TASK = TaskRunner()


def py_cmd(script, *args):
    return [sys.executable, os.path.join(BASE_DIR, script), *[str(a) for a in args]]


def _write_child_export_file(items, prefix):
    os.makedirs(CURRENT_EXPORT_DIR, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=".txt", dir=CURRENT_EXPORT_DIR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for item in items or []:
            raw = str((item or {}).get("raw") or "").strip()
            if raw:
                f.write(raw + "\n")
    return path


def _extract_cmd(accounts, workers=1, headless=True):
    cfg = _load_admin_cfg()
    proxy = str(cfg.get("proxy") or "").strip()
    # ★切回协议版(_export_console_protocol → sub_login_cookie):现已带"加入团队激活"+选企业profile+adpuhao拿码修复 → 4000,
    #   零浏览器、快2-3倍。早期"协议版漏激活只10分"的问题已于 2026-06-17 修复。浏览器版 firefly_login_extract_cookies.py 留作兜底。
    workers = max(1, min(int(workers or 1), _MAX_EXTRACT_WORKERS))
    args = ["_export_console_protocol.py", "--accounts", accounts, "--workers", str(workers)]
    if proxy:
        args += ["--proxy", proxy]
    return py_cmd(*args)


def _selected_console_children(selected):
    items = []
    missing = []
    seen = set()
    for sel in selected:
        children = console_children.get_children(sel)
        if not children:
            missing.append(sel)
            continue
        for child in children:
            raw = str((child or {}).get("raw") or "").strip()
            email = str((child or {}).get("email") or "").strip().lower()
            key = email or raw
            if raw and key not in seen:
                seen.add(key)
                items.append(child)
    return items, missing


def _console_group(sel):
    key = str(sel or "").strip().lower()
    cfg = _load_admin_cfg()
    for c in cfg.get("consoles", []):
        if str(c.get("admin_email") or "").strip().lower() == key or str(c.get("name") or "").strip().lower() == key:
            return str(c.get("group") or "").strip()
    return ""


def _start_console_extract(sel, items, workers=1, headless=True):
    accounts = _write_child_export_file(items, "console_children_")
    group = _console_group(sel)
    # ★只推【本次导出的这些子号】,不重推整个母号(单号导出更关键:否则点1个号却把全母号9个都重推一遍)
    only_emails = [str((it or {}).get("email") or "").strip() for it in (items or []) if str((it or {}).get("email") or "").strip()]

    def after_extract(code):
        if code != 0:
            TASK._emit(f"[推送API] {sel} 导出任务 exit={code}，尝试推送已成功导出的 CK")
        cookie_push.push_console_async(sel, emit=TASK._emit, group=group, only_emails=only_emails)

    return TASK.start_with_callback(
        _extract_cmd(accounts, workers=workers, headless=headless),
        f"导出此母号子号CK: {sel} ({len(items)}个)",
        after_extract,
    )


# 并发安全上限：避免用户把"并发"填太大触发 Adobe 反爬软封 / 拖垮机器
_MAX_EXTRACT_WORKERS = 5


def _start_consoles_extract_merged(selected, workers=1, headless=True):
    """批量导出多个母号的子号CK：合并成【一个】进程跑（workers=全局浏览器上限），
    跑完后逐母号推送。这样总并发 = workers，而不是 母号数 × workers（旧逻辑 80×3=240 卡死的根因）。"""
    present = [s for s in selected if console_children.get_children(s)]
    missing = [s for s in selected if not console_children.get_children(s)]
    if not present:
        return False, "勾选的母号都没有当前子号清单。请先成功执行【删加子号】。", 0, missing
    items, _ = _selected_console_children(present)  # 跨母号去重
    if not items:
        return False, "没有可导出的子号。", 0, missing
    workers = max(1, min(int(workers or 1), _MAX_EXTRACT_WORKERS))
    accounts = _write_child_export_file(items, "consoles_merged_")

    def after_extract(code):
        if code != 0:
            TASK._emit(f"[推送API] 合并导出任务 exit={code}，仍尝试逐母号推送已成功导出的 CK")
        # ★逐母号【串行】推送：50母号一起 async 并发推会瞬间 50 个重请求打爆下游服务器，
        # 每个>20s→全 Read timeout 假失败。改成一个一个串行推，下游不被打爆。
        acc = 0
        for sel in present:
            try:
                rec = cookie_push.push_console_sync(sel, group=_console_group(sel))
                st = rec.get("status")
                if st == "accepted":
                    acc += 1
                    TASK._emit(f"[推送API] {sel} 已接收 {rec.get('sent_count')}/{rec.get('expected_count')} job_id={rec.get('job_id') or '-'}")
                elif st in ("skipped", "unchanged"):
                    TASK._emit(f"[推送API] {sel} 跳过：{rec.get('error') or st}")
                else:
                    TASK._emit(f"[推送API] {sel} {st}：HTTP {rec.get('http_status') or '-'} {rec.get('error') or ''}")
            except Exception as exc:
                TASK._emit(f"[推送API] {sel} 推送异常：{exc}")
        TASK._emit(f"[推送API] 逐母号串行推送完成：accepted {acc}/{len(present)}")

    name = f"批量导出子号CK: {len(present)}个母号 共{len(items)}号 (并发{workers})"
    ok, msg = TASK.start_with_callback(
        _extract_cmd(accounts, workers=workers, headless=headless), name, after_extract
    )
    return ok, msg, len(items), missing


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _load_admin_cfg():
    try:
        with open(ADMIN_CONFIG, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {"proxy": "", "target_seats_per_console": 9, "consoles": []}


def _save_admin_cfg(cfg):
    with open(ADMIN_CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")


def _parse_console_line(line):
    """解析一行母号账号，容错 ---- / | / 空格 分隔，自动识别 RT / client_id。
    Adobe 密码列不固定（有的第2列、有的第3列）→ 存主(第3列优先)+备(另一个密码)两个候选，
    登录时主列报"密码错"会自动换备列重试。"""
    raw = (line or "").strip()
    if not raw or raw.startswith("#"):
        return None
    parts = raw.split("----") if "----" in raw else re.split(r"[|\s,]+", raw)
    parts = [p.strip() for p in parts if p.strip()]
    if not parts or "@" not in parts[0]:
        return None
    email = parts[0]
    rest = parts[1:]
    rt = next((p for p in rest if p.startswith("M.") or (len(p) > 60 and " " not in p)), "")
    cid = next((p for p in rest if _UUID_RE.match(p)), "")
    pwds = [p for p in rest if p not in (rt, cid) and "@" not in p]
    # 主密码：第3列(parts[2])优先；备密码：另一个密码候选（登录时主错了换备重试）
    primary = parts[2] if (len(parts) >= 3 and parts[2] and parts[2] not in (rt, cid)) else (pwds[-1] if pwds else "")
    alt = next((p for p in pwds if p != primary), "")
    return {
        "name": email.split("@")[0],
        "admin_email": email,
        "admin_password": primary,
        "admin_password_alt": alt,
        "refresh_token": rt,
        "client_id": cid,
    }


def _count_lines(path):
    n = 0
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                if line.strip() and not line.strip().startswith("#"):
                    n += 1
    except Exception:
        pass
    return n


def _cookie_count():
    try:
        with open(COOKIES, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        items = data
        if isinstance(data, dict):
            # 文件其实是 {"key": [条目...]} 包装结构 → 取里面的 list 来数
            for v in data.values():
                if isinstance(v, list):
                    items = v
                    break
            else:
                # 退化成 {email: cookie字符串} 平映射
                return len([k for k, v in data.items() if str(v or "").strip()])
        if isinstance(items, list):
            return len([x for x in items if str((x or {}).get("cookie") or "").strip()])
    except Exception:
        pass
    return 0


def _is_seeded(console):
    try:
        # 用"登录成功标记"判，而不是 profile 目录（浏览器一开就建目录，会误报已播种）
        return acm._is_seeded_marker(console)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 路由：页面
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template("index.html")


# --------------------------------------------------------------------------- #
# 路由：邮箱池
# --------------------------------------------------------------------------- #
@app.route("/api/pool")
def api_pool():
    limit = int(request.args.get("limit", 100) or 0)
    data = pool.list_accounts(limit=limit)
    stats = data.get("stats", {})
    # 归类：未用 = available；已开 = success/done/in_use
    used = sum(stats.get(s, 0) for s in ("success", "done", "in_use"))
    summary = {
        "total": stats.get("total", 0),
        "unused": stats.get("available", 0),
        "used": used,
        "failed": stats.get("failed", 0),
        "raw": stats,
    }
    return jsonify({"summary": summary, "items": data.get("items", [])})


@app.route("/api/pool/import", methods=["POST"])
def api_pool_import():
    body = request.get_json(force=True, silent=True) or {}
    text = body.get("text", "")
    mode = body.get("mode", "append")
    records = pool.parse_accounts_text(text)
    if not records:
        return jsonify({"ok": False, "msg": "没解析到有效账号"})
    res = pool.import_accounts(records, mode=mode)
    return jsonify({"ok": True, "result": res})


@app.route("/api/pool/delete", methods=["POST"])
def api_pool_delete():
    body = request.get_json(force=True, silent=True) or {}
    if body.get("all"):
        n = pool.delete_accounts(mode="all")
    else:
        n = pool.delete_accounts(emails=body.get("emails") or [], mode="selected")
    return jsonify({"ok": True, "deleted": n})


@app.route("/api/pool/reset", methods=["POST"])
def api_pool_reset():
    body = request.get_json(force=True, silent=True) or {}
    n = pool.reset_status(statuses=body.get("statuses") or [])
    return jsonify({"ok": True, "reset": n})


@app.route("/api/pool/verify", methods=["POST"])
def api_pool_verify():
    """验活：开浏览器逐个查邮箱池账号 Adobe 是否存在，死号标 deprecated → 删加子号自动跳过。"""
    body = request.get_json(force=True, silent=True) or {}
    args = ["firefly_verify_alive.py", "--workers", str(body.get("workers", 4) or 4)]
    if body.get("limit"):
        args += ["--limit", str(body["limit"])]
    if body.get("headless", True):
        args += ["--headless"]
    cfg = _load_admin_cfg()
    proxy = str(cfg.get("proxy") or "").strip()
    if proxy:
        args += ["--proxy", proxy]
    if cfg.get("use_ip_pool"):
        args += ["--ip-pool"]
    ok, msg = TASK.start(py_cmd(*args), "验活：剔除邮箱池死号")
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/ippool/toggle", methods=["POST"])
def api_ippool_toggle():
    """开/关 IP 池(导cookie+验活每号换干净出口IP,复用产号 _ippool)。返回池状态。"""
    body = request.get_json(force=True, silent=True) or {}
    cfg = _load_admin_cfg()
    cfg["use_ip_pool"] = bool(body.get("on"))
    _save_admin_cfg(cfg)
    st = {}
    try:
        import _proxypool
        if cfg["use_ip_pool"]:
            _proxypool.ensure_core()
        st = _proxypool.status()
    except Exception as exc:
        st = {"ok": False, "error": str(exc)[:100]}
    return jsonify({"ok": True, "use_ip_pool": cfg["use_ip_pool"], "pool": st})


@app.route("/api/ippool/status")
def api_ippool_status():
    cfg = _load_admin_cfg()
    st = {}
    try:
        import _proxypool
        st = _proxypool.status()
    except Exception as exc:
        st = {"ok": False, "error": str(exc)[:100]}
    return jsonify({"use_ip_pool": bool(cfg.get("use_ip_pool")), "pool": st})


# ---------- IP 节点池管理(导入/测试/删除/国家筛选;操作的是与产号共享的同一个池) ----------
_DEFAULT_ALLOW_CC = "US,JP,HK,SG,TW,CA,GB,DE,FR,AU,NL,ES,IT,SE,CH,NO,PL,NZ,IE,AT,BE,DK,FI,PT,LU"


def _ip_allow_list(cfg=None):
    cfg = cfg if cfg is not None else _load_admin_cfg()
    v = cfg.get("pool_allow_cc")
    if isinstance(v, list) and v:
        return [str(c).strip().upper() for c in v if str(c).strip()]
    return _DEFAULT_ALLOW_CC.split(",")


def _apply_ip_country(cfg=None):
    """把选中国家写进 os.environ['FF_ONLY_CC'];导出/验活子进程继承它 → _ippool 只用这些国家的IP。"""
    ccs = _ip_allow_list(cfg)
    os.environ["FF_ONLY_CC"] = ",".join(ccs)
    return ccs


@app.route("/api/ippool/nodes")
def api_ippool_nodes():
    cfg = _load_admin_cfg()
    allow = _ip_allow_list(cfg)
    try:
        import _proxypool
        nodes = _proxypool.nodes_view(allow)
        counts = _proxypool.country_counts()
        core = bool(_proxypool._ipp().core_running())
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)[:160], "nodes": [], "counts": {}, "allow_cc": allow})
    nodes.sort(key=lambda n: (0 if n["healthy"] else 1, n["delay"] if n["delay"] is not None else 999999))
    return jsonify({"ok": True, "nodes": nodes, "counts": counts, "allow_cc": allow,
                    "core_running": core, "total": len(nodes),
                    "healthy": sum(1 for n in nodes if n["healthy"])})


@app.route("/api/ippool/import", methods=["POST"])
def api_ippool_import():
    body = request.get_json(force=True, silent=True) or {}
    sub = str(body.get("sub_url") or "").strip()
    text = str(body.get("nodes_text") or "").strip()
    try:
        import _proxypool
        msgs = []
        if sub:
            a, t = _proxypool.add_subscription(sub)
            msgs.append("订阅新增 %d,池内共 %d" % (a, t))
        if text:
            a, t = _proxypool.add_nodes_text(text)
            msgs.append("粘贴节点新增 %d,池内共 %d" % (a, t))
        if not msgs:
            return jsonify({"ok": False, "msg": "没给订阅URL或节点文本"})
        return jsonify({"ok": True, "msg": "；".join(msgs)})
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)[:160]})


@app.route("/api/ippool/remove", methods=["POST"])
def api_ippool_remove():
    body = request.get_json(force=True, silent=True) or {}
    names = body.get("names") or []
    try:
        import _proxypool
        left = 0
        for nm in names:
            left = _proxypool.remove_node(nm)
        return jsonify({"ok": True, "removed": len(names), "left": left})
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)[:160]})


@app.route("/api/ippool/enable", methods=["POST"])
def api_ippool_enable():
    body = request.get_json(force=True, silent=True) or {}
    try:
        import _proxypool
        _proxypool.set_enabled(str(body.get("name") or ""), bool(body.get("on")))
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)[:160]})


@app.route("/api/ippool/core", methods=["POST"])
def api_ippool_core():
    body = request.get_json(force=True, silent=True) or {}
    try:
        import _proxypool
        if body.get("on"):
            cnt, _pid = _proxypool.start_core()
            return jsonify({"ok": True, "msg": "内核已启动 %d 个端口" % cnt})
        _proxypool.stop_core()
        return jsonify({"ok": True, "msg": "内核已停止"})
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)[:160]})


@app.route("/api/ippool/test", methods=["POST"])
def api_ippool_test():
    ok, msg = TASK.start(py_cmd("_ippool_test.py"), "测IP池：出口IP去重+国家+延迟")
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/ippool/country", methods=["POST"])
def api_ippool_country():
    body = request.get_json(force=True, silent=True) or {}
    ccs = [str(c).strip().upper() for c in (body.get("cc") or []) if str(c).strip()]
    cfg = _load_admin_cfg()
    cfg["pool_allow_cc"] = ccs
    _save_admin_cfg(cfg)
    applied = _apply_ip_country(cfg)
    return jsonify({"ok": True, "allow_cc": applied})


# --------------------------------------------------------------------------- #
# 路由：进度计数
# --------------------------------------------------------------------------- #
@app.route("/api/counts")
def api_counts():
    return jsonify({
        "registered": _count_lines(REGISTERED),
        "added": _count_lines(ADDED),
        "missing": _count_lines(MISSING),
        "cookies": _cookie_count(),
    })


@app.route("/api/cookie/clear", methods=["POST"])
def api_cookie_clear():
    """清空已导 cookie 库（导到外部系统后本地就没用了，省得每次手删）。写 {"items": []}。"""
    try:
        before = _cookie_count()
        with open(COOKIES, "w", encoding="utf-8") as f:
            json.dump({"items": []}, f, ensure_ascii=False)
        return jsonify({"ok": True, "before": before, "msg": f"已清空 cookie 库（原 {before} 条）"})
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)})


# --------------------------------------------------------------------------- #
# 路由：管理员控制台
# --------------------------------------------------------------------------- #
@app.route("/api/consoles")
def api_consoles():
    cfg = _load_admin_cfg()
    child_counts = console_children.counts_by_console()
    out = []
    for c in cfg.get("consoles", []):
        key = str(c.get("admin_email") or c.get("name") or "").strip().lower()
        out.append({
            "name": c.get("name", ""),
            "admin_email": c.get("admin_email", ""),
            "group": c.get("group", ""),
            "product_users_url": c.get("product_users_url", ""),
            "keep_admin_emails": c.get("keep_admin_emails", []),
            "seeded": _is_seeded(c),
            "has_password": bool(c.get("admin_password")),
            "has_token": bool(c.get("admin_refresh_token")),
            "has_url": bool(c.get("product_users_url")),
            "seats": c.get("seats", ""),
            "child_count": child_counts.get(key, 0),
        })
    groups = sorted({str(c.get("group") or "").strip() for c in cfg.get("consoles", []) if str(c.get("group") or "").strip()})
    return jsonify({
        "proxy": cfg.get("proxy", ""),
        "target_seats_per_console": cfg.get("target_seats_per_console", 9),
        "groups": groups,
        "consoles": out,
    })


@app.route("/api/consoles/save", methods=["POST"])
def api_consoles_save():
    body = request.get_json(force=True, silent=True) or {}
    cfg = _load_admin_cfg()
    if "proxy" in body:
        cfg["proxy"] = body["proxy"]
    if "target_seats_per_console" in body:
        cfg["target_seats_per_console"] = int(body["target_seats_per_console"] or 9)
    if "consoles" in body and isinstance(body["consoles"], list):
        cfg["consoles"] = body["consoles"]
    _save_admin_cfg(cfg)
    return jsonify({"ok": True})


@app.route("/api/consoles/add_bulk", methods=["POST"])
def api_consoles_add_bulk():
    """批量添加母号：每行一个账号。只填账号字段，product_users_url/jil_token 留空，
    点【登录母号】时自动提取 org/product/url/token。"""
    body = request.get_json(force=True, silent=True) or {}
    text = body.get("text") or ""
    group = str(body.get("group") or "").strip()
    cfg = _load_admin_cfg()
    existing = {str(c.get("admin_email", "")).strip().lower() for c in cfg.get("consoles", [])}
    added, skipped = [], []
    for line in text.splitlines():
        acc = _parse_console_line(line)
        if not acc:
            continue
        key = acc["admin_email"].lower()
        if key in existing:
            skipped.append(acc["admin_email"])
            continue
        existing.add(key)
        cfg.setdefault("consoles", []).append({
            "name": acc["name"],
            "admin_email": acc["admin_email"],
            "admin_password": acc["admin_password"],
            "admin_password_alt": acc.get("admin_password_alt", ""),
            "admin_refresh_token": acc["refresh_token"],
            "admin_client_id": acc["client_id"],
            "product_users_url": "",
            "jil_token": "",
            "keep_admin_emails": [key],
            "group": group,
        })
        added.append(acc["admin_email"])
    _save_admin_cfg(cfg)
    msg = f"新增 {len(added)} 个母号" + (f"，跳过已存在 {len(skipped)} 个" if skipped else "")
    return jsonify({"ok": True, "added": added, "skipped": skipped, "msg": msg})


@app.route("/api/consoles/set_seats", methods=["POST"])
def api_consoles_set_seats():
    """设置某母号的席位数（子号目标）。空值=清除，回落全局 target_seats_per_console。"""
    body = request.get_json(force=True, silent=True) or {}
    sel = (body.get("console") or "").strip().lower()
    raw = body.get("seats", "")
    cfg = _load_admin_cfg()
    for c in cfg.get("consoles", []):
        if c.get("admin_email", "").strip().lower() == sel or c.get("name", "").strip().lower() == sel:
            if raw in (None, "", "null"):
                c.pop("seats", None)
            else:
                try:
                    c["seats"] = max(0, int(raw))
                except (ValueError, TypeError):
                    return jsonify({"ok": False, "msg": "席位必须是数字"})
            _save_admin_cfg(cfg)
            return jsonify({"ok": True, "seats": c.get("seats", "")})
    return jsonify({"ok": False, "msg": "未找到该母号"})


@app.route("/api/consoles/set_group", methods=["POST"])
def api_consoles_set_group():
    """设置某母号分组。空值=未分组。"""
    body = request.get_json(force=True, silent=True) or {}
    sel = (body.get("console") or "").strip().lower()
    group = str(body.get("group") or "").strip()
    cfg = _load_admin_cfg()
    for c in cfg.get("consoles", []):
        if c.get("admin_email", "").strip().lower() == sel or c.get("name", "").strip().lower() == sel:
            if group:
                c["group"] = group
            else:
                c.pop("group", None)
            _save_admin_cfg(cfg)
            return jsonify({"ok": True, "group": c.get("group", "")})
    return jsonify({"ok": False, "msg": "未找到该母号"})


@app.route("/api/consoles/delete", methods=["POST"])
def api_consoles_delete():
    """删除母号：按管理员邮箱或名称精确匹配，从 config 移除。"""
    body = request.get_json(force=True, silent=True) or {}
    # 支持单个 console 或批量 consoles 列表
    multi = body.get("consoles")
    if isinstance(multi, list) and multi:
        sel_set = {str(x).strip().lower() for x in multi if str(x).strip()}
    else:
        one = (body.get("console") or "").strip().lower()
        sel_set = {one} if one else set()
    if not sel_set:
        return jsonify({"ok": False, "msg": "未指定要删的母号"})
    cfg = _load_admin_cfg()
    before = len(cfg.get("consoles", []))
    cfg["consoles"] = [
        c for c in cfg.get("consoles", [])
        if (c.get("admin_email", "").strip().lower() not in sel_set
            and c.get("name", "").strip().lower() not in sel_set)
    ]
    _save_admin_cfg(cfg)
    removed = before - len(cfg["consoles"])
    return jsonify({"ok": removed > 0, "removed": removed,
                    "msg": (f"已删除 {removed} 个母号" if removed else "没匹配到要删的母号")})


@app.route("/api/consoles/raw")
def api_consoles_raw():
    return jsonify(_load_admin_cfg())


@app.route("/api/console/quota", methods=["POST"])
def api_console_quota():
    """查某母号下每个子号的 Firefly 积分额度(只读、不顶登录态)。
    用子号自己的 cookie 直连 Firefly 查(与 adobe2api 算 2090/4000 同一套),跟号在不在 adobe2api 库无关。"""
    body = request.get_json(force=True, silent=True) or {}
    sel = str(body.get("console") or "").strip()
    children = console_children.get_children(sel)
    if not children:
        return jsonify({"ok": False, "msg": "这个母号还没有当前子号清单。请先成功执行【删加子号】。"})
    try:
        import _quota
    except Exception as exc:
        return jsonify({"ok": False, "msg": "查额度模块加载失败: %s" % exc})
    try:
        cookie_map = cookie_push._load_cookie_map()
    except Exception:
        cookie_map = {}
    proxy = str(_load_admin_cfg().get("proxy") or "").strip() or None

    def _q(child):
        email = str((child or {}).get("email") or "").strip()
        row = cookie_map.get(email.lower()) if email else None
        ck = (row or {}).get("cookie") or ""
        if not ck:
            return {"email": email, "has_cookie": False, "token_ok": False,
                    "available": None, "total": None, "reason": "库里无cookie"}
        try:
            r = _quota.query_quota(ck, proxy=proxy)
        except Exception as exc:
            r = {"token_ok": False, "available": None, "total": None, "reason": "查询异常:%s" % str(exc)[:60]}
        # ★代理刷不出 token(高并发撞代理/Adobe限流抖动)→ 直连兜底重试一次,避免把活号误标"死号"
        if proxy and not r.get("token_ok"):
            try:
                r2 = _quota.query_quota(ck, proxy=None)
                if r2.get("token_ok"):
                    r = r2
            except Exception:
                pass
        r["email"] = email
        r["has_cookie"] = True
        return r

    from concurrent.futures import ThreadPoolExecutor
    workers = max(1, min(10, len(children)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        quotas = list(ex.map(_q, children))
    alive = sum(1 for q in quotas if q.get("token_ok"))
    return jsonify({"ok": True, "console": sel, "count": len(quotas), "alive": alive, "quotas": quotas})


@app.route("/api/console/quota_one", methods=["POST"])
def api_console_quota_one():
    """单刷一个子号额度(带直连兜底,不受批量高并发限流影响,用来核实"死号"真假)。body:{email}。"""
    body = request.get_json(force=True, silent=True) or {}
    email = str(body.get("email") or "").strip()
    if not email:
        return jsonify({"ok": False, "msg": "缺 email"})
    import _quota
    try:
        cookie_map = cookie_push._load_cookie_map()
    except Exception:
        cookie_map = {}
    proxy = str(_load_admin_cfg().get("proxy") or "").strip() or None
    ck = (cookie_map.get(email.lower()) or {}).get("cookie") or ""
    if not ck:
        return jsonify({"ok": True, "email": email, "has_cookie": False, "token_ok": False,
                        "available": None, "total": None, "reason": "库里无cookie"})
    try:
        r = _quota.query_quota(ck, proxy=proxy)
    except Exception as exc:
        r = {"token_ok": False, "available": None, "total": None, "reason": "查询异常:%s" % str(exc)[:60]}
    if proxy and not r.get("token_ok"):  # 代理失败→直连兜底
        try:
            r2 = _quota.query_quota(ck, proxy=None)
            if r2.get("token_ok"):
                r = r2
        except Exception:
            pass
    r["email"] = email
    r["has_cookie"] = True
    r["ok"] = True
    return jsonify(r)


@app.route("/api/console/export_one", methods=["POST"])
def api_console_export_one():
    """单独导出【一个子号】的 cookie(协议登录→写本地池→推adobe)。用于"库里无cookie"/批量卡429的号
    单独补导,避开批量并发限流。body:{console, email}。复用 _start_console_extract(单号)。"""
    body = request.get_json(force=True, silent=True) or {}
    sel = str(body.get("console") or "").strip()
    email = str(body.get("email") or "").strip().lower()
    if not email:
        return jsonify({"ok": False, "msg": "缺 email"})
    items = console_children.get_children(sel) or []
    one = [it for it in items if str((it or {}).get("email") or "").strip().lower() == email]
    if not one:
        return jsonify({"ok": False, "msg": "该子号不在当前清单(可能没存账密),无法登录导出"})
    if not str((one[0] or {}).get("raw") or "").strip():
        return jsonify({"ok": False, "msg": "该子号没有账密(raw),无法协议登录导出"})
    ok, msg = _start_console_extract(sel, one, workers=1, headless=True)
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/console/child_remove", methods=["POST"])
def api_console_child_remove():
    """从本地子号清单删掉号(死号/幽灵号清理,纯本地、不碰 Adobe)。body:{items:[{email,console}]} 或 {email,console}。"""
    body = request.get_json(force=True, silent=True) or {}
    items = body.get("items")
    if not items:
        items = [{"email": body.get("email"), "console": body.get("console")}]
    by_con = {}
    for it in items:
        em = str((it or {}).get("email") or "").strip()
        con = str((it or {}).get("console") or "").strip()
        if em:
            by_con.setdefault(con, []).append(em)
    total = 0
    for con, ems in by_con.items():
        total += console_children.remove_children(con, ems)
    return jsonify({"ok": True, "removed": total})


@app.route("/api/console/fill_seats", methods=["POST"])
def api_console_fill_seats():
    """检测母号 Adobe 真实席位,不满就从邮箱池补差额(★只加不删、保留现有号)+协议导新号cookie。
    body:{dry_run, seats, workers, console, no_extract}。走后台任务,看实时日志。"""
    body = request.get_json(force=True, silent=True) or {}
    args = ["_fill_seats.py", "--workers", str(int(body.get("workers", 3) or 3))]
    if body.get("seats"):
        args += ["--seats", str(int(body["seats"]))]
    if str(body.get("console") or "").strip():
        args += ["--console", str(body["console"]).strip()]
    if body.get("dry_run"):
        args += ["--dry-run"]
    if body.get("no_extract"):
        args += ["--no-extract"]
    name = ("席位检测(只看缺口)" if body.get("dry_run") else "席位补满(缺的补差额+导cookie)")
    ok, msg = TASK.start(py_cmd(*args), name)
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/console/sync_children", methods=["POST"])
def api_console_sync_children():
    """从 Adobe 真实成员同步回本地子号清单(修复🗑误删真号、清单<Adobe)。body:{dry_run, console, workers}。"""
    body = request.get_json(force=True, silent=True) or {}
    args = ["_sync_children.py", "--workers", str(int(body.get("workers", 3) or 3))]
    if str(body.get("console") or "").strip():
        args += ["--console", str(body["console"]).strip()]
    if body.get("dry_run"):
        args += ["--dry-run"]
    name = ("同步清单(只看)" if body.get("dry_run") else "从Adobe同步子号清单")
    ok, msg = TASK.start(py_cmd(*args), name)
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/console/swap", methods=["POST"])
def api_console_swap():
    """换号(破坏性,走后台任务):删指定旧子号 + 从邮箱池加等量新 + 导出新子号CK + 推adobe。
    body: {swaps:[{console, old:[email...]}], dry_run, then_extract, workers}。旧子号被踢后在adobe2api自动变死号。"""
    body = request.get_json(force=True, silent=True) or {}
    swaps = [s for s in (body.get("swaps") or []) if s.get("console") and (s.get("old") or [])]
    if not swaps:
        return jsonify({"ok": False, "msg": "没有要换的子号(先在子号控制台勾选)"})
    dry = bool(body.get("dry_run"))
    then_extract = bool(body.get("then_extract", True))
    import tempfile
    base = os.path.dirname(os.path.abspath(__file__))
    fd, path = tempfile.mkstemp(prefix="swaps_", suffix=".json", dir=base)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(swaps, f, ensure_ascii=False)
    args = ["admin_jil_swap.py", "--swaps-file", path, "--workers", str(int(body.get("workers", 3) or 3)),
            "--console-workers", str(int(body.get("console_workers", 3) or 3))]
    if dry:
        args += ["--dry-run"]
    elif then_extract:
        args += ["--then-extract"]
        if not bool(body.get("push", True)):   # ★一键换选中:换+导cookie进池,不推adobe(推送交给导出门禁)
            args += ["--no-push"]
    n = sum(len(s.get("old") or []) for s in swaps)
    name = ("(DRY-RUN)" if dry else "") + f"换号: {n}个子号 / {len(swaps)}个母号"
    ok, msg = TASK.start(py_cmd(*args), name)
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/console/autoswap", methods=["POST"])
def api_console_autoswap():
    """全自动换低分号(走后台任务):扫所有子号→找<阈值分→删旧+加新+导出+只推新号。"""
    body = request.get_json(force=True, silent=True) or {}
    threshold = int(body.get("threshold", 100) or 100)
    dry = bool(body.get("dry_run"))
    args = ["_autoswap.py", "--threshold", str(threshold),
            "--workers", str(int(body.get("workers", 3) or 3)),
            "--export-delay", str(int(body.get("export_delay", 0) or 0))]
    if body.get("limit"):
        args += ["--limit", str(int(body["limit"]))]
    if dry:
        args += ["--dry-run"]
    name = ("(DRY)" if dry else "") + f"全自动换<{threshold}分子号"
    ok, msg = TASK.start(py_cmd(*args), name)
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/console/protocol_import", methods=["POST"])
def api_console_protocol_import():
    """🚀 协议全流程导入(纯HTTP、零浏览器):母号协议登录→JIL列真团队子号→协议导出cookie→推adobe2api。
    body: {console(母号email), limit(限子号数,0=全量), push, check_credits, workers}。"""
    body = request.get_json(force=True, silent=True) or {}
    console = str(body.get("console") or "").strip()
    if not console:
        return jsonify({"ok": False, "msg": "需指定母号(填 admin_email)"})
    limit = int(body.get("limit", 0) or 0)
    args = ["full_flow_protocol.py", console, "--workers", str(int(body.get("workers", 3) or 3)),
            "--limit", str(limit if limit > 0 else 9999)]
    if body.get("check_credits", True):
        args += ["--check-credits"]
    if body.get("push", True):
        args += ["--push"]
    name = f"🚀协议全流程导入: {console}" + (f"(前{limit}个)" if limit else "(全量)")
    ok, msg = TASK.start(py_cmd(*args), name)
    return jsonify({"ok": ok, "msg": msg})


# --- 导出卖号(adobe2api JSON) + 标记已导出(account_id 防撞号) ---
@app.route("/api/sell/stats")
def api_sell_stats():
    """账本:本地池/已导出/可卖 + 已导出email集合(前端子号控制台标'已售')。"""
    import _export_a2a
    try:
        return jsonify({"ok": True, **_export_a2a.sell_stats()})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)[:120]})


@app.route("/api/sell/export", methods=["POST"])
def api_sell_export():
    """导出 N 个未导出的号 → adobe2api json 当附件下载;同时标记已导出(防重复卖)。"""
    import _export_a2a
    body = request.get_json(force=True, silent=True) or {}
    limit = int(body.get("limit", 0) or 0)
    re_export = bool(body.get("re_export"))
    min_total = int(body.get("min_total", 1000) or 1000)
    try:
        fname, out, remain, skipped = _export_a2a.do_export(limit=limit, re_export=re_export, min_total=min_total)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)[:160]})
    if not out["items"]:
        extra = ("(普号已拦 %d 个,无企业积分不卖)" % skipped) if skipped else "(都导过了;勾'重导'可连已导的也导)"
        return jsonify({"ok": False, "msg": "没有可导出的企业号 " + extra})
    data = json.dumps(out, ensure_ascii=False, indent=2)
    resp = Response(data, mimetype="application/json; charset=utf-8")
    resp.headers["Content-Disposition"] = 'attachment; filename="%s"' % fname
    resp.headers["X-Export-Count"] = str(out["total"])
    resp.headers["X-Export-Remain"] = str(remain)
    resp.headers["X-Export-Skipped"] = str(skipped)
    resp.headers["Access-Control-Expose-Headers"] = "X-Export-Count,X-Export-Remain,X-Export-Skipped,Content-Disposition"
    return resp


@app.route("/api/sell/purge_dead", methods=["POST"])
def api_sell_purge_dead():
    """清死号:实时验证本地cookie池,移除确实死掉的号(积分0/invalid);429/查不出的一律保留(防误删)。
    默认只验"可卖"那批(快,修正可卖计数);?full=1 验全池(慢,清历史已售死号缩池)。"""
    import _export_a2a
    body = request.get_json(force=True, silent=True) or {}
    full = bool(body.get("full"))
    try:
        r = _export_a2a.purge_dead(only_unexported=(not full), workers=5, log=TASK._emit)
        return jsonify({"ok": True, "removed": r["removed"], "checked": r["checked"],
                        "msg": f"清掉 {r['removed']} 个死号(查了{r['checked']}个,429/查不出的保留)",
                        **_export_a2a.sell_stats()})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)[:160]})


@app.route("/api/sell/ledger")
def api_sell_ledger():
    """已售追踪台账:按卖出日期分组(日期+卖时基线);?current=1 时并发查当前积分算"已用/是否未用"(慢)。"""
    import _export_a2a
    with_current = request.args.get("current") in ("1", "true", "yes")
    try:
        return jsonify({"ok": True, **_export_a2a.ledger_view(with_current=with_current)})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)[:160]})


@app.route("/api/sell/ledger/backfill", methods=["POST"])
def api_sell_ledger_backfill():
    """把历史已导出号回填进台账(便于追踪老号当前用量)。"""
    import _export_a2a
    try:
        return jsonify({"ok": True, **_export_a2a.backfill_ledger()})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)[:160]})


# --- 3P 探测打标:探不通的废号自动从本地池删除,只让能用的进生产+导出 ---
@app.route("/api/probe3p/run", methods=["POST"])
def api_probe3p_run():
    """3P探测(后台任务):每号探3P(veo cost预检·不扣积分),不可用(408/无权益/超时)的号按 action 处理。
    body:{workers, dry_run, action(swap=换号默认/delete=删池), export_delay}。dry_run=只探不动先看会处理哪些。"""
    body = request.get_json(force=True, silent=True) or {}
    action = "delete" if body.get("action") == "delete" else "swap"
    args = ["_probe3p.py", "--workers", str(int(body.get("workers", 12) or 12)), "--action", action,
            "--export-delay", str(int(body.get("export_delay", 0) or 0))]
    dry = bool(body.get("dry_run"))
    if dry:
        args += ["--dry-run"]
    name = ("(DRY)" if dry else "") + ("3P探测换号" if action == "swap" else "3P探测删池")
    ok, msg = TASK.start(py_cmd(*args), name)
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/probe3p/stats")
def api_probe3p_stats():
    """最近一次3P探测结果摘要(可用数/本次删数/累计删数/可用号清单)。"""
    import _probe3p
    try:
        return jsonify({"ok": True, **_probe3p.stats()})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)[:160]})


# --- 真实408探测:每号真实打 generate-async 看实际状态码(200能出图/403无权益/408被甩),只检测不动号 ---
@app.route("/api/probe408/run", methods=["POST"])
def api_probe408_run():
    """真实408探测(后台任务):对每个有cookie子号真实提交 firefly-3p generate-async,看Adobe网关到底返回
    200(能出图)/403(org无3P权益)/408(org被风控甩负载)。★真实提交、能出图的号会排1张图;只检测不换不删。"""
    body = request.get_json(force=True, silent=True) or {}
    args = ["_probe408.py", "--workers", str(int(body.get("workers", 5) or 5))]
    ok, msg = TASK.start(py_cmd(*args), "真实408探测")
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/probe408/stats")
def api_probe408_stats():
    """最近一次真实408探测结果摘要(200/408/403数)+ 每号打标。"""
    import _probe408
    try:
        return jsonify({"ok": True, **_probe408.stats()})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)[:160]})


@app.route("/api/sell/mark", methods=["POST"])
def api_sell_mark():
    """上传【已发客户的旧导出 json】,用 account_id 认号标记进 exported_accounts.txt(防重复卖)。"""
    import _mark_exported
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "msg": "没选文件"})
    fd, tmp = tempfile.mkstemp(prefix="_mark_", suffix=".json")
    os.close(fd)
    try:
        f.save(tmp)
        st = _mark_exported.do_mark([tmp])
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)[:160]})
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass
    return jsonify({"ok": True, **st})


@app.route("/api/sell/mark_emails", methods=["POST"])
def api_sell_mark_emails():
    """手动把选中的子号 email 标记已售/取消已售(写 exported_accounts.txt,导出时跳过)。body:{emails:[], unmark}。"""
    import _export_a2a
    body = request.get_json(force=True, silent=True) or {}
    emails = [str(e).strip().lower() for e in (body.get("emails") or []) if str(e).strip()]
    unmark = bool(body.get("unmark"))
    if not emails:
        return jsonify({"ok": False, "msg": "没选号(先在列表勾选子号)"})
    cur = _export_a2a.load_exported()
    sel = set(emails)
    if unmark:
        changed = len(cur & sel)
        cur -= sel
    else:
        changed = len(sel - cur)
        cur |= sel
    with open(_export_a2a.EXPORTED_FILE, "w", encoding="utf-8") as f:
        for e in sorted(cur):
            f.write(e + "\n")
    return jsonify({"ok": True, "action": ("取消已售" if unmark else "标记已售"),
                    "changed": changed, **_export_a2a.sell_stats()})


# --- 定时自动换号:开启后持续后台,每 interval 秒扫一遍、换 <threshold 分;app 重启自动恢复 ---
import threading as _threading

_AUTOSWAP_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "autoswap_state.json")
_AUTOSWAP = {"enabled": False, "interval": 600, "threshold": 100, "last_run": 0, "last_msg": "未开启"}


def _save_autoswap_state():
    try:
        with open(_AUTOSWAP_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"enabled": _AUTOSWAP["enabled"], "interval": _AUTOSWAP["interval"], "threshold": _AUTOSWAP["threshold"]}, f)
    except Exception:
        pass


def _autoswap_loop():
    import time as _t
    while _AUTOSWAP["enabled"]:
        try:
            if TASK.running:
                _AUTOSWAP["last_msg"] = "有任务在跑,本轮跳过(下轮再试)"
            else:
                a = ["_autoswap.py", "--threshold", str(_AUTOSWAP["threshold"]), "--workers", "3", "--export-delay", "0"]
                ok, msg = TASK.start(py_cmd(*a), "定时自动换<%d分子号" % _AUTOSWAP["threshold"])
                _AUTOSWAP["last_run"] = int(_t.time())
                _AUTOSWAP["last_msg"] = "已启动一轮(看实时日志)" if ok else ("启动失败:%s" % msg)
        except Exception as exc:
            _AUTOSWAP["last_msg"] = "异常 %s" % str(exc)[:80]
        slept = 0
        while slept < _AUTOSWAP["interval"] and _AUTOSWAP["enabled"]:
            _t.sleep(5)
            slept += 5
    _AUTOSWAP["last_msg"] = "已停止"


def _start_autoswap_loop():
    _threading.Thread(target=_autoswap_loop, daemon=True).start()


@app.route("/api/console/autoswap/toggle", methods=["POST"])
def api_autoswap_toggle():
    body = request.get_json(force=True, silent=True) or {}
    _AUTOSWAP["threshold"] = int(body.get("threshold", 100) or 100)
    _AUTOSWAP["interval"] = max(60, int(body.get("interval", 600) or 600))
    enable = bool(body.get("enabled"))
    if enable and not _AUTOSWAP["enabled"]:
        _AUTOSWAP["enabled"] = True
        _start_autoswap_loop()
    elif not enable:
        _AUTOSWAP["enabled"] = False
    _save_autoswap_state()
    return jsonify({"ok": True, "enabled": _AUTOSWAP["enabled"], "interval": _AUTOSWAP["interval"], "threshold": _AUTOSWAP["threshold"]})


@app.route("/api/console/autoswap/status")
def api_autoswap_status():
    return jsonify({"enabled": _AUTOSWAP["enabled"], "interval": _AUTOSWAP["interval"],
                    "threshold": _AUTOSWAP["threshold"], "last_run": _AUTOSWAP["last_run"], "last_msg": _AUTOSWAP["last_msg"]})


# --------------------------------------------------------------------------- #
# 路由：执行动作（都走单任务执行器，输出到日志）
# --------------------------------------------------------------------------- #
@app.route("/api/admin/seed", methods=["POST"])
def api_admin_seed():
    """手动登录：headed 真人登一次，确认到页面后显示已登录、等手工关闭。"""
    body = request.get_json(force=True, silent=True) or {}
    console = body.get("console", "")
    ok, msg = TASK.start(py_cmd("admin_seed_login.py", console), f"手动登录: {console or '第一个'}")
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/console/open_admin", methods=["POST"])
def api_console_open_admin():
    """打开母号页(纯查看)：headed 用母号已播种 session 直接进 Adobe Admin Console 看状态/是否被封,看完手工关。"""
    body = request.get_json(force=True, silent=True) or {}
    console = (body.get("console") or "").strip()
    ok, msg = TASK.start(py_cmd("admin_open_page.py", console), f"打开母号页: {console or '第一个'}")
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/admin/login", methods=["POST"])
def api_admin_login():
    """自动登录：全程自动(密码+refresh_token,headless)，登录后自动关闭并缓存 session。"""
    body = request.get_json(force=True, silent=True) or {}
    console = body.get("console", "")
    args = ["admin_console_manage.py", "--login-only", "--headless"]
    if console:
        args += ["--console", console]
    ok, msg = TASK.start(py_cmd(*args), f"自动登录: {console or '全部'}")
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/admin/run", methods=["POST"])
def api_admin_run():
    body = request.get_json(force=True, silent=True) or {}
    args = ["admin_console_manage.py"]
    if body.get("console"):
        args += ["--console", body["console"]]
    if body.get("dry_run"):
        args += ["--dry-run"]
    else:
        args += ["--headless"]
    if body.get("seats"):
        args += ["--seats", body["seats"]]
    if body.get("add_file"):
        args += ["--add-file", body["add_file"]]
    if body.get("reset_added"):
        args += ["--reset-added"]
    if body.get("then_extract"):
        args += ["--then-extract"]
    name = ("Dry-Run" if body.get("dry_run") else "批量删/加") + f": {body.get('console') or '全部'}"
    ok, msg = TASK.start(py_cmd(*args), name)
    return jsonify({"ok": ok, "msg": msg})


def _find_console(sel):
    cfg = _load_admin_cfg()
    s = (sel or "").strip().lower()
    for c in cfg.get("consoles", []):
        if not s or s in str(c.get("name", "")).lower() or s in str(c.get("admin_email", "")).lower():
            return c
    return None


@app.route("/api/admin/umapi_test", methods=["POST"])
def api_umapi_test():
    """测 UMAPI 连接 + 列出 product profiles（同步，立即返回）。"""
    body = request.get_json(force=True, silent=True) or {}
    c = _find_console(body.get("console", ""))
    if not c:
        return jsonify({"ok": False, "msg": "没找到该管理员"})
    org_id = c.get("org_id") or umapi.org_id_from_url(c.get("product_users_url", ""))
    try:
        res = umapi.test_connection(org_id, c.get("umapi_client_id", ""), c.get("umapi_client_secret", ""))
        return jsonify({"ok": True, "org_id": org_id, "profiles": res["profiles"]})
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)})


@app.route("/api/admin/umapi", methods=["POST"])
def api_umapi_run():
    """UMAPI 批量删/加（后台任务，输出到日志）。"""
    body = request.get_json(force=True, silent=True) or {}
    args = ["admin_umapi_manage.py"]
    if body.get("console"):
        args += ["--console", body["console"]]
    if body.get("dry_run"):
        args += ["--dry-run"]
    if body.get("seats"):
        args += ["--seats", str(body["seats"])]
    if body.get("then_extract"):
        args += ["--then-extract"]
    name = ("UMAPI Dry-Run" if body.get("dry_run") else "UMAPI 批量删/加") + f": {body.get('console') or '全部'}"
    ok, msg = TASK.start(py_cmd(*args), name)
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/admin/jil_test", methods=["POST"])
def api_jil_test():
    """只读验证 JIL 会话 token + 列 license groups（同步立即返回）。"""
    body = request.get_json(force=True, silent=True) or {}
    c = _find_console(body.get("console", ""))
    if not c:
        return jsonify({"ok": False, "msg": "没找到该管理员"})
    org_id = c.get("org_id") or jil.org_id_from_url(c.get("product_users_url", ""))
    product_id = c.get("product_id") or jil.product_id_from_url(c.get("product_users_url", ""))
    token = c.get("jil_token", "")
    sel = c.get("admin_email") or c.get("name") or ""
    if not token:
        # 没 token：直接自动用已播种 session 后台刷新
        ok, msg = TASK.start(py_cmd("admin_jil_refresh_token.py", "--console", sel), f"自动刷新Token: {sel}")
        if ok:
            return jsonify({"ok": False, "auto_refreshing": True,
                            "msg": "还没 token，已自动【协议登录】后台抓取(不开浏览器，约30-60秒、含拿邮箱码)。等日志显示刷新完成后，再点一次【登录母号】即可。"})
        return jsonify({"ok": False, "msg": f"没 token 且无法自动刷新（{msg}）。请先【登录母号/播种】一次。"})
    try:
        res = jil.test_token(org_id, product_id, token)
        return jsonify({"ok": True, "org_id": org_id, "product_id": product_id, "license_groups": res["license_groups"]})
    except Exception as exc:
        # token 失效：自动后台刷新
        ok, msg = TASK.start(py_cmd("admin_jil_refresh_token.py", "--console", sel), f"自动刷新Token: {sel}")
        if ok:
            return jsonify({"ok": False, "auto_refreshing": True,
                            "msg": f"token 已失效，已自动【协议登录】后台刷新(不开浏览器)。等日志显示刷新完成后，再点一次【登录母号】验证。"})
        return jsonify({"ok": False, "msg": f"token 失效，且无法自动刷新（{msg}）。"})


@app.route("/api/admin/jil", methods=["POST"])
def api_jil_run():
    """JIL 批量加微软号（后台任务）。"""
    body = request.get_json(force=True, silent=True) or {}
    args = ["admin_jil_manage.py"]
    if body.get("console"):
        args += ["--console", body["console"]]
    if body.get("dry_run"):
        args += ["--dry-run"]
    if body.get("seats"):
        args += ["--seats", str(body["seats"])]
    if body.get("add_file"):
        args += ["--add-file", body["add_file"]]
    if body.get("then_extract"):
        args += ["--then-extract"]
    sel_list = body.get("consoles") or []
    if sel_list:
        args += ["--only", ",".join(str(x) for x in sel_list)]
    if body.get("workers"):
        args += ["--workers", str(body["workers"])]
    scope = (f"{len(sel_list)} 个勾选" if sel_list else (body.get("console") or "全部"))
    name = ("扫描子号" if body.get("dry_run") else "删加子号") + f": {scope}"
    ok, msg = TASK.start(py_cmd(*args), name)
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/admin/login_discover", methods=["POST"])
def api_login_discover():
    """登录母号并自动提取 org/product/url/token（后台浏览器任务）。"""
    body = request.get_json(force=True, silent=True) or {}
    args = ["admin_login_discover.py"]
    if body.get("console"):
        args += ["--console", body["console"]]
    if body.get("protocol_only"):
        args += ["--protocol-only"]   # 母号协议开关ON:纯协议零浏览器,失败不回退浏览器
    elif body.get("headed"):
        args += ["--headed"]
    if body.get("workers"):
        args += ["--workers", str(body["workers"])]
    sel_list = body.get("consoles") or []
    if sel_list:
        args += ["--only", ",".join(str(x) for x in sel_list)]
    label = f"{len(sel_list)} 个勾选" if sel_list else (body.get("console") or "全部")
    ok, msg = TASK.start(py_cmd(*args), f"登录母号+提取JSON: {label}")
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/admin/jil_refresh", methods=["POST"])
def api_jil_refresh():
    """自动刷新母号 JIL token（后台任务，用已播种 session 抓新 Bearer 写回 config）。"""
    body = request.get_json(force=True, silent=True) or {}
    args = ["admin_jil_refresh_token.py"]
    if body.get("console"):
        args += ["--console", body["console"]]
    ok, msg = TASK.start(py_cmd(*args), f"刷新Token: {body.get('console') or '全部'}")
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/admin/pool")
def api_admin_pool():
    """母号号池:实时从 config 读全部母号账密(改密/换RT后立刻更新),供查看+直接复制。
    返回 rows(结构化,含org/席位/子号数)+ copy_lines(可复制:邮箱----Adobe密码----邮箱密码----cid----RT)。"""
    import admin_console_manage as _acm
    try:
        d = json.load(open(_acm.CONFIG_FILE, encoding="utf-8-sig"))
    except Exception as e:
        return jsonify({"ok": False, "msg": "读config失败:%s" % str(e)[:60]})
    rows, lines = [], []
    for c in d.get("consoles", []):
        em = c.get("admin_email") or ""
        if not em:
            continue
        adobe_pw = c.get("admin_password") or ""
        mail_pw = c.get("admin_password_alt") or ""
        cid = c.get("admin_client_id") or ""
        rt = c.get("admin_refresh_token") or ""
        rows.append({"name": c.get("name") or "", "email": em, "adobe_password": adobe_pw,
                     "email_password": mail_pw, "client_id": cid, "has_rt": bool(rt), "rt_len": len(rt),
                     "org_id": c.get("org_id") or "", "seats": c.get("seats"),
                     "seeded": bool(_acm._is_seeded_marker(c) if hasattr(_acm, "_is_seeded_marker") else False)})
        lines.append("----".join([em, adobe_pw, mail_pw, cid, rt]))
    return jsonify({"ok": True, "count": len(rows), "rows": rows, "copy_text": "\n".join(lines)})


@app.route("/api/admin/security", methods=["POST"])
def api_admin_security():
    """母号安全处置(后台任务):协议改Adobe密码+全局登出 / 全自动换RT / 两者。零手动。
    body:{console(邮箱/名称/逗号分隔/'all'), action:change-password|refresh-rt|both}。"""
    body = request.get_json(force=True, silent=True) or {}
    console = str(body.get("console") or "").strip()
    action = str(body.get("action") or "change-password").strip()
    if action not in ("change-password", "refresh-rt", "both"):
        return jsonify({"ok": False, "msg": "action 非法"})
    sel_list = body.get("consoles") or []
    target = ",".join(str(x) for x in sel_list) if sel_list else (console or "all")
    args = ["admin_security.py", "--console", target, "--action", action]
    label = {"change-password": "改Adobe密码+全局登出", "refresh-rt": "全自动换RT", "both": "改密+换RT"}[action]
    ok, msg = TASK.start(py_cmd(*args), f"{label}: {target}")
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/extract/console", methods=["POST"])
def api_extract_console():
    body = request.get_json(force=True, silent=True) or {}
    sel = body.get("console", "")
    items = console_children.get_children(sel)
    if not items:
        return jsonify({"ok": False, "msg": "这个母号还没有当前子号清单。请先成功执行一次【删加子号】。"})
    workers = int(body.get("workers", 1) or 1)
    headless = bool(body.get("headless", True))
    ok, msg = _start_console_extract(sel, items, workers=workers, headless=headless)
    return jsonify({"ok": ok, "msg": msg, "count": len(items)})


@app.route("/api/extract/consoles", methods=["POST"])
def api_extract_consoles():
    body = request.get_json(force=True, silent=True) or {}
    selected = [str(x).strip() for x in (body.get("consoles") or []) if str(x).strip()]
    if not selected:
        return jsonify({"ok": False, "msg": "请先勾选要导出子号 CK 的母号。"})

    workers = int(body.get("workers", 1) or 1)
    headless = bool(body.get("headless", True))
    # 合并成一个进程跑（全局并发=workers），不再 母号数×workers
    ok, msg, total, missing = _start_consoles_extract_merged(selected, workers=workers, headless=headless)
    if not ok:
        return jsonify({"ok": False, "msg": msg})
    warn = f"；跳过 {len(missing)} 个无当前子号清单母号" if missing else ""
    return jsonify({"ok": True, "msg": msg + warn, "count": total, "started": 1, "missing": missing})


@app.route("/api/extract/run", methods=["POST"])
def api_extract_run():
    body = request.get_json(force=True, silent=True) or {}
    accounts = body.get("accounts") or "__current_children_all__"
    workers = int(body.get("workers", 1) or 1)
    headless = bool(body.get("headless", True))
    if accounts == "__current_children_all__" or accounts.startswith("__group__"):
        cfg = _load_admin_cfg()
        grp = accounts[len("__group__"):].strip() if accounts.startswith("__group__") else None  # ★只导该分组的母号
        requested = [str(x).strip().lower() for x in (body.get("consoles") or []) if str(x).strip()]
        selected = []
        for c in (cfg.get("consoles") or []):
            key = str((c or {}).get("admin_email") or (c or {}).get("name") or "").strip()
            if not key:
                continue
            if grp is not None and str((c or {}).get("group") or "").strip() != grp:
                continue
            if requested and key.lower() not in requested and str((c or {}).get("name") or "").strip().lower() not in requested:
                continue
            selected.append(key)
        selected = [x for x in selected if x]
        # 合并成一个进程跑（全局并发=workers），不再 母号数×workers
        ok, msg, total, _missing = _start_consoles_extract_merged(selected, workers=workers, headless=headless)
        if not ok:
            return jsonify({"ok": False, "msg": msg})
        return jsonify({"ok": True, "msg": msg, "count": total, "started": 1})
    else:
        name = f"导出Cookie: {accounts}"
    ok, msg = TASK.start(_extract_cmd(accounts, workers=workers, headless=headless), name)
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/push/config", methods=["GET", "POST"])
def api_push_config():
    if request.method == "GET":
        return jsonify(cookie_push.public_config())
    body = request.get_json(force=True, silent=True) or {}
    cfg = cookie_push.save_config(body)
    return jsonify({"ok": True, **cookie_push.public_config(), "api_key_set": bool(cfg.get("api_key"))})


@app.route("/api/push/records")
def api_push_records():
    limit = int(request.args.get("limit", 200) or 200)
    return jsonify({"records": cookie_push.load_records(limit=limit)})


@app.route("/api/push/records/clear", methods=["POST"])
def api_push_records_clear():
    cookie_push.clear_records()
    TASK._emit("[推送API] 推送记录已清空")
    return jsonify({"ok": True})


@app.route("/api/push/console", methods=["POST"])
def api_push_console():
    body = request.get_json(force=True, silent=True) or {}
    sel = str(body.get("console") or "").strip()
    if not sel:
        return jsonify({"ok": False, "msg": "缺少母号"})
    # 手动推送：用户明确点了，force=True 绕过"内容相同"去重；ignore_master=True 不受推送总开关限制
    rec = cookie_push.push_console_sync(sel, group=_console_group(sel), force=True, ignore_master=True)
    return jsonify({"ok": rec.get("status") in ("accepted", "partial"), "record": rec, "msg": rec.get("error") or rec.get("status")})


# --------------------------------------------------------------------------- #
# 路由：任务控制 + 日志
# --------------------------------------------------------------------------- #
@app.route("/api/task/status")
def api_task_status():
    return jsonify({"running": TASK.running, "name": TASK.name})


@app.route("/api/task/stop", methods=["POST"])
def api_task_stop():
    return jsonify({"ok": TASK.stop()})


@app.route("/api/logs")
def api_logs():
    def stream():
        q = TASK.subscribe()
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    line = q.get(timeout=15)
                    yield f"data: {json.dumps(line, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"
        except GeneratorExit:
            pass
        finally:
            TASK.unsubscribe(q)
    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/logs/clear", methods=["POST"])
def api_logs_clear():
    return jsonify({"ok": TASK.clear_logs()})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5006"))  # 协议版用 5006,避免和团队工具(5005)冲突
    # 应用上次选中的 IP 池国家(写进 FF_ONLY_CC,导出/验活子进程继承)
    try:
        _ccs = _apply_ip_country()
        print("[IP池] 国家筛选：%s" % ",".join(_ccs), flush=True)
    except Exception as _e:
        print("[IP池] 国家应用失败 %s" % _e, flush=True)
    # 恢复定时自动换号(上次开着的话,重启后自动续上)
    try:
        if os.path.exists(_AUTOSWAP_STATE_FILE):
            _st = json.load(open(_AUTOSWAP_STATE_FILE, encoding="utf-8"))
            if _st.get("enabled"):
                _AUTOSWAP["threshold"] = int(_st.get("threshold", 100) or 100)
                _AUTOSWAP["interval"] = max(60, int(_st.get("interval", 600) or 600))
                _AUTOSWAP["enabled"] = True
                _start_autoswap_loop()
                print("[定时换号] 已恢复:每 %ds 换 <%d 分" % (_AUTOSWAP["interval"], _AUTOSWAP["threshold"]), flush=True)
    except Exception as _e:
        print("[定时换号] 恢复失败 %s" % _e, flush=True)
    host = os.environ.get("HOST", "127.0.0.1")   # 服务器公网部署设 HOST=0.0.0.0(务必配 panel_auth.json 门禁)
    print(f"Web 控制台启动： http://{host}:{port}", flush=True)
    app.run(host=host, port=port, threaded=True, debug=False)
