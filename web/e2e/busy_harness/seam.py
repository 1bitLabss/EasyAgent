"""Send across the last 2s of a MEDIUM reply and the first 1s after it finishes.

Twenty offsets. A message during the reply pauses it. A message during the save
does not pause, copy, or wedge that reply. Either way the new message is answered.
"""

import json
import sys
import time

sys.argv = [sys.argv[0], sys.argv[1]] + sys.argv[2:]
from b import *  # noqa: E402

OFFSETS = [round(-2.0 + i * (3.0 / 19), 3) for i in range(20)]


def _idle(view):
    status = view.status()
    button = view.btn()
    if button.startswith("Send") and not status.strip().startswith(
        ("Writing", "Paused", "Waiting", "Working", "Running", "Thinking", "Queued")
    ):
        running, _state = view.srv_running()
        return not running
    return False


def _calibrate(pg, view):
    view.rec(True)
    started = time.time()
    view.send("MEDIUM please calibrate")
    while time.time() - started < 90:
        if _idle(view):
            return time.time() - started
        pg.wait_for_timeout(50)
    raise SystemExit("calibration reply did not finish")


def main():
    os.makedirs(SH, exist_ok=True)
    failed = []
    with sync_playwright() as playwright:
        browser = launch(playwright)
        ctx = browser.new_context(viewport={"width": 1280, "height": 850})
        ctx.add_init_script(INIT)
        page = ctx.new_page()
        page.goto(B)
        page.wait_for_timeout(1200)
        calib_bot = new_bot("Qseamcal")
        page.reload()
        page.wait_for_timeout(1200)
        sample = S(page, calib_bot, "Qseamcal")
        sample.open()
        duration = _calibrate(page, sample)
        ctx.close()
        print(json.dumps({"calibrated_s": round(duration, 2)}), flush=True)
        for offset in OFFSETS:
            name = f"S{offset:+.3f}"
            tag = f"{name}{int(time.time()) % 10000}"
            bot_id = new_bot("Q" + tag)
            ctx = browser.new_context(viewport={"width": 1280, "height": 850})
            ctx.add_init_script(INIT)
            page = ctx.new_page()
            page.goto(B)
            page.wait_for_timeout(1000)
            view = S(page, bot_id, "Q" + tag)
            view.open()
            view.rec(True)
            out = {"case": name, "kind": "time", "arg": offset, "calibrated_s": round(duration, 2)}
            try:
                started = time.time()
                view.send(f"MEDIUM please {tag}")
                if offset <= 0:
                    target = max(0.2, duration + offset)
                    while time.time() - started < target:
                        page.wait_for_timeout(20)
                else:
                    while time.time() - started < duration + 20:
                        if _idle(view):
                            break
                        page.wait_for_timeout(30)
                    page.wait_for_timeout(int(offset * 1000))
                out["trigger_status"] = view.status()
                out["trigger_after_s"] = round(time.time() - started, 2)
                live_q = page.evaluate(
                    "()=>{const r=[...document.querySelectorAll('[data-testid=transcript] .message-row[data-role=assistant]')]; return r.length?qa(r[r.length-1]):null}"
                )
                at_send = view.last_bot_len()
                token = f"MSG-{tag}"
                sent = view.send(f"SHORT busy {token}")
                page.wait_for_timeout(700)
                out["paused_mark"] = page.evaluate(
                    "()=>[...document.querySelectorAll('[data-testid=paused-mark]')].some(n=>(n.textContent||'').includes('paused'))"
                )
                out["idle_s"] = view.wait_idle()
                frames = view.frames()
                out["an"] = analyze(frames, [(token, sent)], live_q, at_send)
                out["cmp"] = compare(page, bot_id)
                out["cmp_final"] = out["cmp"]
                out["nothing_in_flight_text"] = "nothing was in flight" in page.evaluate(
                    "()=>document.querySelector('[data-testid=transcript]').textContent"
                )
                out["watch"] = page.evaluate("()=>window.__eaWatch||[]")
                out["long_tasks"] = page.evaluate("()=>window.__long||[]")
            except Exception as exc:
                out["error"] = repr(exc)[:400]
            bad = problems_for(out)
            roles = (out.get("cmp") or {}).get("dom_roles") or []
            if out.get("idle_s") is not None and roles and roles[-1] != "assistant":
                bad.append("the new message got no answer")
            out["problems"] = bad
            if bad:
                failed.append((name, bad))
            print(json.dumps({"case": name, "problems": bad, "trigger": out.get("trigger_status"), "idle_s": out.get("idle_s"), "gap": (out.get("an") or {}).get("max_frame_gap_ms"), "finish": (out.get("an") or {}).get("finish_gap_px")}), flush=True)
            ctx.close()
        browser.close()
    if failed:
        print("FAIL", json.dumps(failed), flush=True)
        sys.exit(1)
    print("PASS seam", flush=True)


if __name__ == "__main__":
    main()
