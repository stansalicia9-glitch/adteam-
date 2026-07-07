# -*- coding: utf-8 -*-
"""SSH 到 adobe2api 生产服务器跑命令(经 Clash 7890 SOCKS5)。一次连接跑多条,避开 sshd 限流。
用法: echo/heredoc 把命令(每行一条)从 stdin 喂进来。"""
import sys
import socks
import paramiko

HOST = "103.195.102.125"
PORT = 22
USER = "root"
PW = "miaomiao5211314"


USE_PROXY = False  # 直连就能拿到 banner，省去代理延迟导致的握手超时


def _connect():
    import time
    last = None
    waits = [0, 25, 45, 70, 100, 140, 180]
    for attempt in range(len(waits)):
        if waits[attempt]:
            time.sleep(waits[attempt])
        try:
            sock = None
            if USE_PROXY:
                sock = socks.socksocket()
                sock.set_proxy(socks.SOCKS5, "127.0.0.1", 7890)
                sock.settimeout(45)
                sock.connect((HOST, PORT))
            cli = paramiko.SSHClient()
            cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            cli.connect(HOST, port=PORT, username=USER, password=PW, sock=sock,
                        timeout=45, banner_timeout=60, auth_timeout=45,
                        look_for_keys=False, allow_agent=False)
            return cli
        except Exception as exc:
            last = exc
            print(f"[connect attempt {attempt+1} failed: {exc}; backing off]", flush=True)
    raise last


def run(script):
    cli = _connect()
    _in, out, err = cli.exec_command(script, timeout=600)
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    if o:
        print(o, flush=True)
    if e.strip():
        print("[stderr] " + e, flush=True)
    cli.close()


if __name__ == "__main__":
    run(sys.stdin.read())
