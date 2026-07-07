# -*- coding: utf-8 -*-
"""持久化 profile 探针：登一次缓存 session（后续秒进），探查 Add users 按钮/弹窗/复选框真实 DOM。
非破坏：不 Save、不真删（勾选后立即取消）。"""
import os, re, sys, json
from playwright.sync_api import sync_playwright
import firefly_register_yescaptcha as firefly
import admin_console_manage as acm

DBG = os.path.join(acm.BASE_DIR, "firefly_debug")
PROFILE = os.path.join(acm.BASE_DIR, "admin_profile")
os.makedirs(DBG, exist_ok=True)

def shot(page, name):
    try:
        page.screenshot(path=os.path.join(DBG, f"probe_{name}.png"), full_page=False, timeout=8000)
        print(f"[shot] probe_{name}", flush=True)
    except Exception as e:
        print(f"[shot-fail] {name}: {e}", flush=True)

def dump_buttons(page, label):
    try:
        info = page.evaluate(r"""() => {
            const want = ['add users','remove users','add licenses','save','add'];
            const vis = n => { const b=n.getBoundingClientRect(); const s=getComputedStyle(n);
                return b.width>0&&b.height>0&&s.display!=='none'&&s.visibility!=='hidden'; };
            const out=[];
            for (const n of document.querySelectorAll('button,[role=button],a,sp-button,coral-button,input[type=button],input[type=submit]')) {
              if(!vis(n)) continue;
              const t=(n.innerText||n.textContent||n.value||n.getAttribute('aria-label')||'').replace(/\s+/g,' ').trim();
              if(!t) continue;
              if(!want.some(w=>t.toLowerCase().includes(w))) continue;
              const b=n.getBoundingClientRect();
              out.push({text:t, tag:n.tagName, role:n.getAttribute('role')||'', cls:(n.className||'').toString().slice(0,80),
                        disabled:n.disabled===true||n.getAttribute('aria-disabled')==='true',
                        x:Math.round(b.x+b.width/2), y:Math.round(b.y+b.height/2)});
            }
            return out;
        }""")
        print(f"[buttons:{label}] {json.dumps(info, ensure_ascii=False)}", flush=True)
        return info
    except Exception as e:
        print(f"[buttons:{label}] err {e}", flush=True); return []

def dialog_open(page):
    try:
        return bool(page.evaluate(r"""() => {
            const t=(document.body.innerText||'').toLowerCase();
            const dlg=document.querySelector('[role=dialog],coral-dialog,.spectrum-Dialog,[class*=Modal]');
            return (!!dlg) || t.includes('add users to') || t.includes('assign users') || t.includes('add user to');
        }"""))
    except Exception:
        return False

def dump_dialog_inputs(page):
    try:
        info = page.evaluate(r"""() => {
            const vis=n=>{const b=n.getBoundingClientRect();const s=getComputedStyle(n);return b.width>0&&b.height>0&&s.display!=='none'&&s.visibility!=='hidden';};
            const out=[];
            for(const n of document.querySelectorAll('input,textarea,[contenteditable=true]')){
              if(!vis(n))continue; const t=String(n.type||'').toLowerCase();
              if(['checkbox','radio','hidden'].includes(t))continue;
              const b=n.getBoundingClientRect();
              out.push({tag:n.tagName,type:t,ph:n.placeholder||'',aria:n.getAttribute('aria-label')||'',id:(n.id||'').slice(0,40),
                        w:Math.round(b.width),h:Math.round(b.height),ce:!!n.isContentEditable});
            }
            return out;
        }""")
        print(f"[dialog-inputs] {json.dumps(info, ensure_ascii=False)}", flush=True)
        return info
    except Exception as e:
        print(f"[dialog-inputs] err {e}", flush=True); return []

def main():
    cfg, consoles = acm._load_consoles(); console=consoles[0]
    proxy=(cfg.get("proxy") or "").strip() or None
    with sync_playwright() as p:
        ctx = acm._launch_admin_context(p, proxy, headless=True, profile_dir=acm._console_profile(console))
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            ok = acm._login_admin(page, ctx, console, proxy, console.get("name","c"), timeout=200)
            print("login ok:", ok, flush=True)
            if not ok:
                shot(page,"login_fail"); return
            acm._wait_for_users_table(page, 25000); shot(page,"00_table")
            print("rows:", json.dumps(acm._collect_rows(page), ensure_ascii=False), flush=True)
            dump_buttons(page,"table")

            # --- 用 Playwright 原生 role 点击 Add users ---
            print("=== click Add users (native role) ===", flush=True)
            clicked=False
            for strat in ["role","text"]:
                try:
                    if strat=="role":
                        page.get_by_role("button", name=re.compile(r"add users", re.I)).first.click(timeout=4000)
                    else:
                        page.get_by_text(re.compile(r"^\s*Add users\s*$", re.I)).first.click(timeout=4000)
                    clicked=True; print(f"clicked via {strat}", flush=True); break
                except Exception as e:
                    print(f"click {strat} fail: {str(e)[:80]}", flush=True)
            acm._wait(page,2500); shot(page,"01_after_addclick")
            print("dialog_open:", dialog_open(page), flush=True)
            dump_dialog_inputs(page)

            # 试输入邮箱
            acm._type_email_into_dialog(page,"probe_test_demo@outlook.com","probe"); acm._wait(page,1500)
            shot(page,"02_after_type"); dump_dialog_inputs(page)
            try: page.keyboard.press("Escape")
            except Exception: pass
            acm._wait(page,1000)

            # --- 测试复选框（勾上看 Remove users 是否启用，再取消）---
            print("=== checkbox test ===", flush=True)
            rows=acm._collect_rows(page)
            nonadmin=[r["email"] for r in rows if r["email"].lower() not in set(console["keep_admin_emails"])]
            if nonadmin:
                em=nonadmin[0]
                set1=acm._set_row_checkbox(page, em, True); acm._wait(page,800)
                shot(page,"03_checked"); dump_buttons(page,"after_check")
                acm._set_row_checkbox(page, em, False)
                print(f"checkbox set {em}: {set1}", flush=True)
        except Exception as e:
            print("FATAL", e, flush=True); shot(page,"99_fatal")
        finally:
            try: ctx.close()
            except Exception: pass

if __name__=="__main__":
    main()
