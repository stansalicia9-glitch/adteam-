# -*- coding: utf-8 -*-
import json, concurrent.futures as cf
import _quota

cc = json.load(open("console_children.json", encoding="utf-8")).get("consoles", {})
ck = json.load(open("firefly_adobe2api_cookies.json", encoding="utf-8"))
items = ck.get("items", ck) if isinstance(ck, dict) else ck
ckmap = {str(i.get("name", "")).strip().lower(): (i.get("cookie") or "") for i in items}

tasks = []
for con, kids in cc.items():
    for c in kids:
        em = str(c.get("email", "")).strip().lower()
        cookie = ckmap.get(em, "")
        if cookie:
            tasks.append((con, em, cookie))
print("待查 %d 个有CK的子号" % len(tasks), flush=True)

def q(t):
    con, em, cookie = t
    r = _quota.query_quota(cookie)
    return (con, em, r.get("token_ok"), r.get("available"), r.get("total"))

res = []
with cf.ThreadPoolExecutor(max_workers=12) as ex:
    res = list(ex.map(q, tasks))

low = [r for r in res if r[2] and (r[3] is not None) and r[3] <= 0]
low.sort(key=lambda r: (r[3] if r[3] is not None else 9e9))
print("=== 0/无额度子号(token活但可用<=0) ===", flush=True)
for con, em, ok, av, tt in low[:30]:
    print("  %-42s 母号=%-40s 积分=%s/%s" % (em, con, av, tt), flush=True)
print("共 %d 个 0/无额度" % len(low), flush=True)
