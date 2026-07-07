# -*- coding: utf-8 -*-
"""测 IP 节点池出口IP(团队工具用,作为 TASK 子进程跑,进度流式打到实时日志)。
启动 mihomo 内核 → 并发测每个 enabled 节点出口IP(去重)+ 国家 + Adobe 直连延迟 → 写回共享 state。
操作的是与产号共享的同一个池(_ippool),不重开第二个池。"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import _proxypool


def main():
    p = _proxypool._ipp()
    print("启动 mihomo 内核(生成多端口配置,每节点一个本地端口=固定出口IP)…", flush=True)
    cnt, pid = p.start_core()
    if not cnt:
        print("❌ 没有启用的节点,先【导入订阅/节点】再测。", flush=True)
        return 1
    print("✅ 内核已启动:%d 个端口 pid=%s" % (cnt, pid), flush=True)
    print("开始并发测出口IP(去重)+ 国家 + Adobe 延迟,稍等…", flush=True)

    def prog(i, total, name, ip, cc, ms):
        nm = (name or "")[:28]
        tail = ("%sms" % ms) if ms else "不通/慢"
        print("  [%d/%d] %-28s -> %s %s %s" % (i, total, nm, ip or "(无出口)", cc or "", tail), flush=True)

    uniq, total = p.test_exit_ips(progress=prog)
    print("=" * 56, flush=True)
    print("✅ 测试完成:去重出口IP %d 个 / 测试节点 %d 个(重复IP的节点已标记 dup,不进轮询池)" % (uniq, total), flush=True)
    print("现在可在面板看每个节点的国家/出口IP/延迟,并按国家筛选。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
