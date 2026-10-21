"""Ten long replies back to back. A frame gap over a second is a freeze."""

import json
import sys
import time

sys.argv = [sys.argv[0], sys.argv[1]] + sys.argv[2:]
from b import *  # noqa: E402


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
        tag = "L" + str(int(time.time()) % 10000)
        bot_id = new_bot("Q" + tag)
        page.reload()
        page.wait_for_timeout(1200)
        view = S(page, bot_id, "Q" + tag)
        view.open()
        for index in range(10):
            view.rec(True)
            started = time.time()
            view.send(f"LONG plain run {index} {tag}")
            idle = view.wait_idle(400)
            page.wait_for_timeout(400)
            frames = view.frames()
            spaces = [frames[i]["t"] - frames[i - 1]["t"] for i in range(1, len(frames))]
            gap = round(max(spaces)) if spaces else None
            quiet = [
                frame.get("gap") or 0
                for frame in frames
                if (frame.get("st") or "").startswith(("Idle", "Waiting"))
            ]
            finish = max(quiet) if quiet else 0
            doc = saved(bot_id)
            answer = doc["messages"][-1]
            row = {
                "i": index,
                "secs": round(time.time() - started, 1),
                "idle_wait": idle,
                "saved_len": len(answer.get("content") or ""),
                "interrupted": bool(answer.get("interrupted")),
                "max_frame_gap_ms": gap,
                "finish_gap_px": finish,
                "long_tasks": page.evaluate("()=>(window.__long||[]).slice(-8)"),
                "watch": page.evaluate("()=>window.__eaWatch||[]"),
            }
            bad = []
            if idle is None:
                bad.append("did not go idle")
            if gap is None or gap > 1000:
                bad.append(f"frame gap {gap}ms")
            if finish > 2:
                bad.append(f"finish jump {finish}px")
            if row["interrupted"]:
                bad.append("long reply was interrupted")
            if row["saved_len"] < 8000:
                bad.append(f"short reply {row['saved_len']}")
            row["problems"] = bad
            if bad:
                failed.append(row)
            print(json.dumps(row), flush=True)
        browser.close()
    if failed:
        print("FAIL", json.dumps(failed), flush=True)
        sys.exit(1)
    print("PASS long10", flush=True)


if __name__ == "__main__":
    main()
