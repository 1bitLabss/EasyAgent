"""The 19 busy-send cases. Exit 1 unless every case stays still and pauses on time."""

import json
import os
import sys
import time

sys.argv = [sys.argv[0], sys.argv[1]] + sys.argv[2:]
from b import *  # noqa: E402

only = sys.argv[2].split(",") if len(sys.argv) > 2 else None
CASES = [
    ("E1", "LONG", "chars", 200),
    ("E2", "LONG", "chars", 450),
    ("E3", "LONG", "chars", 900),
    ("M1", "LONG", "chars", 2000),
    ("M2", "LONG", "chars", 3500),
    ("M3", "LONG", "chars", 5000),
    ("M4", "LONG", "time", 22),
    ("T1", "TOOLSTEP", "tool", 2),
    ("T2", "TOOLSTEP", "tool", 5),
    ("N1", "MEDIUM", "time", 33.0),
    ("N2", "MEDIUM", "time", 33.8),
    ("N3", "MEDIUM", "time", 34.6),
    ("BU", "LONG", "burst", 2500),
    ("AE", "MEDIUM", "after_end", 0),
    ("RL", "LONG", "reload", 2500),
    ("AE2", "MEDIUM", "after_end", 0),
    ("AE3", "MEDIUM", "after_end", 0),
    ("M5", "LONG", "chars", 2000),
]
os.makedirs(SH, exist_ok=True)
res = open(os.path.join(SH, "cases.jsonl"), "a")
failed = []
with sync_playwright() as p:
    br = launch(p)
    for name, prompt, kind, arg in CASES:
        if only and name not in only:
            continue
        tag = f"{name}{int(time.time()) % 10000}"
        bid = new_bot("Q" + tag)
        ctx = br.new_context(viewport={"width": 1280, "height": 850})
        ctx.add_init_script(INIT)
        pg = ctx.new_page()
        pg.goto(B)
        pg.wait_for_timeout(1500)
        view = S(pg, bid, "Q" + tag)
        view.open()
        view.rec(True)
        out = {"case": name, "kind": kind, "arg": arg}
        try:
            view.send(f"{prompt} please {tag}")
            t0 = time.time()
            trig = None
            if kind == "after_end":
                out["first_idle_s"] = view.wait_idle()
            else:
                while time.time() - t0 < 120:
                    length = view.last_bot_len()
                    status = view.status()
                    if kind in ("chars", "burst", "reload") and length >= arg:
                        break
                    if kind == "time" and time.time() - t0 >= arg:
                        break
                    if kind == "tool" and ("sleep" in status or "erminal" in status or "Running" in status or "command" in status.lower()):
                        if trig is None:
                            trig = time.time()
                        if time.time() - trig >= arg:
                            break
                    pg.wait_for_timeout(50)
                out["trigger_status"] = view.status()
                out["trigger_after_s"] = round(time.time() - t0, 1)
            if kind == "reload":
                before = view.last_bot_len()
                pg.screenshot(path=SH + f"{name}-before-reload.png")
                pg.reload()
                tr = time.time()
                view.open()
                view.rec(True)
                seen = None
                while time.time() - tr < 15:
                    length = view.last_bot_len()
                    if length >= 0.8 * before:
                        seen = round(time.time() - tr, 2)
                        break
                    pg.wait_for_timeout(50)
                pg.wait_for_timeout(2000)
                out["reload"] = {
                    "len_before": before,
                    "visible_after_s": seen,
                    "len_2s_later": view.last_bot_len(),
                    "status": view.status(),
                    "btn": view.btn(),
                }
                pg.screenshot(path=SH + f"{name}-after-reload.png")
            live_q = pg.evaluate(
                "()=>{const r=[...document.querySelectorAll('[data-testid=transcript] .message-row[data-role=assistant]')]; return r.length?qa(r[r.length-1]):null}"
            )
            at_send = view.last_bot_len()
            sends = []
            if kind == "burst":
                for i in range(3):
                    sends.append((f"MSG-{tag}b{i}", view.send(f"SHORT burst {i} MSG-{tag}b{i}")))
                    pg.wait_for_timeout(150)
            else:
                sends.append((f"MSG-{tag}", view.send(f"SHORT busy MSG-{tag}")))
            pg.wait_for_timeout(700)
            pg.screenshot(path=SH + f"{name}-0.7s-after-send.png")
            out["paused_mark"] = pg.evaluate(
                "()=>[...document.querySelectorAll('[data-testid=paused-mark]')].some(n=>(n.textContent||'').includes('paused'))"
            )
            out["idle_s"] = view.wait_idle()
            pg.wait_for_timeout(1500)
            pg.screenshot(path=SH + f"{name}-answered.png")
            frames = view.frames()
            json.dump(
                {"sends": sends, "live_q": live_q, "F": frames},
                open(os.path.join(SH, f"frames-{name}-{tag}.json"), "w"),
            )
            out["an"] = analyze(frames, sends, live_q if kind != "after_end" else None, at_send)
            out["cmp"] = compare(pg, bid)
            out["after"] = {"status": view.status(), "btn": view.btn()}
            follow = []
            for k in range(2):
                view.rec(True)
                token = f"MSG-{tag}f{k}"
                sent_at = view.send(f"SHORT follow {k} {token}")
                pg.wait_for_timeout(400)
                button = view.btn()
                status = view.status()
                idle = view.wait_idle(60)
                pg.wait_for_timeout(800)
                frames = view.frames()
                report = analyze(frames, [(token, sent_at)], None, 0)
                row = report[token]
                follow.append(
                    {
                        "appear_ms": row["appear_ms"],
                        "dup": row["vanish_dup_events"],
                        "max_step_px": row["max_step_px"],
                        "existing_rows_max_step_px": report.get("existing_rows_max_step_px", 0),
                        "assistant_len_drops": report.get("assistant_len_drops", 0),
                        "btn_running": button,
                        "st_running": status,
                        "idle_s": idle,
                        "btn_after": view.btn(),
                        "st_after": view.status(),
                    }
                )
            out["follow"] = follow
            out["cmp_final"] = compare(pg, bid)
            dom_all = pg.evaluate("()=>document.querySelector('[data-testid=transcript]').textContent")
            out["nothing_in_flight_text"] = "nothing was in flight" in dom_all
        except Exception as exc:
            out["error"] = repr(exc)[:400]
            pg.screenshot(path=SH + f"{name}-error.png")
        bad = problems_for(out)
        out["problems"] = bad
        if bad:
            failed.append((name, bad))
        res.write(json.dumps(out) + "\n")
        res.flush()
        print(json.dumps({"case": name, "problems": bad, "pause_ms": (out.get("an") or {}).get("pause_ms"), "idle_s": out.get("idle_s")}), flush=True)
        ctx.close()
    br.close()
if failed:
    print("FAIL", json.dumps(failed), flush=True)
    sys.exit(1)
print("PASS 19", flush=True)
