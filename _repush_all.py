# -*- coding: utf-8 -*-
"""把已导出的子号CK 逐母号【串行】重新推送到 adobe 服务器。
之前 50母号并发推把服务器打爆→全 Read timeout 假失败；这里一个一个串行推 + 180s 超时。
force=False：上次已真正接收(accepted)的同内容母号自动跳过，只补没推上去的。"""
import json
import time

import cookie_push

cfg = json.load(open("admin_console_config.json", encoding="utf-8"))
consoles = []
for c in (cfg.get("consoles") or []):
    key = str((c or {}).get("admin_email") or (c or {}).get("name") or "").strip()
    if key:
        consoles.append(key)

print(f"#### 逐母号串行重推: {len(consoles)} 个母号 ####", flush=True)
acc = skip = fail = 0
t0 = time.time()
for i, sel in enumerate(consoles, 1):
    try:
        rec = cookie_push.push_console_sync(sel)  # force=False
        st = rec.get("status")
        if st == "accepted":
            acc += 1
        elif st in ("skipped", "unchanged"):
            skip += 1
        else:
            fail += 1
        print(
            f"[{i}/{len(consoles)}] {sel} -> {st} "
            f"sent={rec.get('sent_count')}/{rec.get('expected_count')} "
            f"http={rec.get('http_status')} {str(rec.get('error') or '')[:90]}",
            flush=True,
        )
    except Exception as exc:
        fail += 1
        print(f"[{i}/{len(consoles)}] {sel} -> EXC {exc}", flush=True)
    time.sleep(1)
print(f"#### 完成: accepted {acc} / skip {skip} / fail {fail} ; 耗时 {time.time()-t0:.0f}s ####", flush=True)
