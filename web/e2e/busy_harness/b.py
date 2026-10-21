"""Playwright helpers for the busy-send harness.

The page records one frame per animation frame. A later pass decides whether
a row that was already on screen moved.
"""

import json
import os
import re
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

SH = os.environ.get("BUSY_HARNESS_OUT", "/tmp/busy-harness/")
B = os.environ.get("BUSY_HARNESS_URL", "http://127.0.0.1:8891")
EID = sys.argv[1] if len(sys.argv) > 1 else ""

INIT = r"""
window.__ids=new WeakMap(); window.__n=0; window.__F=[]; window.__rec=false; window.__long=[];
window.qa=function(e){ if(!__ids.has(e)) __ids.set(e, ++__n); return __ids.get(e); };
try {
  const obs=new PerformanceObserver((list)=>{ for (const e of list.getEntries()) { if (e.duration>=50) window.__long.push([Math.round(e.startTime), Math.round(e.duration), e.name]); } });
  obs.observe({type:'longtask', buffered:true});
} catch (e) {}
function frame(){ if(window.__rec){ const s=document.querySelector('.chat-scroll'); const sr=s?s.getBoundingClientRect():null;
  const rows=[...document.querySelectorAll('[data-testid=transcript] .message-row[data-role]')].map(r=>{const b=r.getBoundingClientRect(); return [qa(r), r.getAttribute('data-role'), r.getAttribute('data-message-id')||'', (r.textContent||'').length, Math.round(b.top-sr.top+s.scrollTop), (r.textContent||'').slice(0,80)]});
  const rs=document.querySelector('[data-testid=run-state]'); const btn=document.querySelector('.send-btn');
  __F.push({t:performance.now(), rows, st:rs?rs.textContent:'', btn:btn?(btn.getAttribute('aria-label')+(btn.disabled?'/dis':'')):'', gap:s?Math.round(s.scrollHeight-s.scrollTop-s.clientHeight):null}); }
  requestAnimationFrame(frame); }
requestAnimationFrame(frame);
"""


def api(path, data=None, method=None):
    rq = urllib.request.Request(
        B + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    return json.load(urllib.request.urlopen(rq))


def new_bot(name):
    bot = api("/api/bots", {"name": name, "endpoint_id": EID})
    return bot["id"]


def saved(bid):
    cid = api(f"/api/bots/{bid}/ongoing")["id"]
    return api(f"/api/bots/{bid}/chats/{cid}?window=200")


def launch(playwright):
    return playwright.chromium.launch(channel="chrome")


class S:
    def __init__(self, pg, bid, name):
        self.pg = pg
        self.bid = bid
        self.name = name
        self.ta = pg.locator("#draft")

    def open(self):
        self.pg.get_by_role("button", name=re.compile("^" + self.name + "(,.*)?$")).first.click()
        self.pg.wait_for_timeout(1200)

    def rec(self, on):
        flag = "true" if on else "false"
        self.pg.evaluate(f"()=>{{window.__rec={flag}; if({flag}) window.__F=[]}}")

    def now(self):
        return self.pg.evaluate("performance.now()")

    def send(self, text):
        self.ta.fill(text)
        t = self.now()
        self.pg.keyboard.press("Enter")
        return t

    def last_bot_len(self):
        return self.pg.evaluate(
            "()=>{const r=[...document.querySelectorAll('[data-testid=transcript] .message-row[data-role=assistant]')];return r.length?r[r.length-1].textContent.length:0}"
        )

    def status(self):
        return self.pg.evaluate("()=>{const r=document.querySelector('[data-testid=run-state]');return r?r.textContent:''}")

    def btn(self):
        return self.pg.evaluate("()=>{const b=document.querySelector('.send-btn');return b.getAttribute('aria-label')+(b.disabled?'/dis':'')}")

    def srv_running(self):
        doc = saved(self.bid)
        run = doc.get("run") or {}
        status = run.get("status") or run.get("state")
        return status == "running", status

    def wait_idle(self, to=240):
        t0 = time.time()
        while time.time() - t0 < to:
            st_ = self.status()
            button = self.btn()
            if button.startswith("Send") and not st_.strip().startswith(
                ("Writing", "Paused", "Waiting", "Working", "Running", "Thinking", "Queued")
            ):
                if not self.srv_running()[0]:
                    return round(time.time() - t0, 1)
            self.pg.wait_for_timeout(250)
        return None

    def frames(self):
        return self.pg.evaluate("()=>window.__F")


def analyze(F, sends, live_q, live_len_at_send, pause_needed=True):
    del pause_needed
    result = {}
    t0 = sends[0][1]
    for tok, ts in sends:
        first = None
        qid = None
        bad = []
        tops = []
        for frame in F:
            if frame["t"] < ts:
                continue
            found = [row for row in frame["rows"] if row[1] == "user" and tok in row[5]]
            if first is None and found:
                first = frame["t"] - ts
                qid = found[0][0]
            if first is not None:
                if len(found) != 1:
                    bad.append((round(frame["t"] - ts), len(found)))
                elif found[0][0] != qid:
                    bad.append((round(frame["t"] - ts), "newnode"))
                else:
                    tops.append(found[0][4])
        deltas = [abs(tops[i] - tops[i - 1]) for i in range(1, len(tops))]
        result[tok] = {
            "appear_ms": round(first, 1) if first is not None else None,
            "vanish_dup_events": len(bad),
            "bad_sample": bad[:3],
            "max_step_px": max(deltas) if deltas else 0,
            "top_range_px": (max(tops) - min(tops)) if tops else None,
        }
    pauses = [frame["t"] - t0 for frame in F if frame["t"] >= t0 and "Paused, reading" in (frame["st"] or "")]
    result["pause_ms"] = round(pauses[0], 1) if pauses else None
    pre = [frame["st"] for frame in F if frame["t"] < t0]
    result["status_pre_send"] = sorted(set(re.sub(r"\d+", "#", item) for item in pre))[:4]
    if live_q:
        lens = [(frame["t"] - t0, next((row[3] for row in frame["rows"] if row[0] == live_q), None)) for frame in F if frame["t"] >= t0]
        gone = [round(t) for t, length in lens if length is None]
        result["partial"] = {
            "len_at_send": live_len_at_send,
            "len_end": lens[-1][1] if lens else None,
            "frames_missing": len(gone),
            "min_len_after": min([length for _, length in lens if length is not None], default=None),
        }
    base = [frame for frame in F if frame["t"] < t0]
    if base:
        ids0 = [row[0] for row in base[-1]["rows"]]
        moved = 0
        prev = {}
        for frame in F:
            if frame["t"] < t0:
                continue
            for row in frame["rows"]:
                if row[0] in ids0:
                    if row[0] in prev:
                        moved = max(moved, abs(row[4] - prev[row[0]]))
                    prev[row[0]] = row[4]
        result["existing_rows_max_step_px"] = moved
    seen = {}
    flicker = 0
    for frame in F:
        current = {row[0] for row in frame["rows"]}
        for key in list(seen):
            if key not in current:
                seen[key] = "gone"
        for key in current:
            if seen.get(key) == "gone":
                flicker += 1
            seen[key] = "on"
    result["row_reappear_events"] = flicker
    drops = 0
    last = {}
    for frame in F:
        for row in frame["rows"]:
            if row[1] == "assistant":
                if row[0] in last and row[3] < last[row[0]] - 20:
                    drops += 1
                last[row[0]] = row[3]
    result["assistant_len_drops"] = drops
    result["gap_max_after"] = max([frame["gap"] or 0 for frame in F if frame["t"] >= t0], default=None)
    spaces = [frame["t"] - F[index - 1]["t"] for index, frame in enumerate(F) if index]
    result["max_frame_gap_ms"] = round(max(spaces)) if spaces else 0
    quiet = [
        frame.get("gap") or 0
        for frame in F
        if (frame.get("st") or "").startswith("Idle") or (frame.get("st") or "").startswith("Waiting")
    ]
    result["finish_gap_px"] = max(quiet) if quiet else 0
    return result


def compare(pg, bid):
    doc = saved(bid)
    messages = doc.get("messages", [])
    stale = [item.get("content", "")[:50] for item in messages if item.get("kind") == "status"]
    stored = [
        (
            item.get("role"),
            item.get("id") or item.get("client_id") or "",
            (item.get("content") or "")[:30].replace("\n", " "),
            bool(item.get("interrupted")),
            len(item.get("content") or ""),
        )
        for item in messages
        if item.get("kind") not in ("status", "pickup", "activity")
        and item.get("about") not in ("activity", "queue")
        and (item.get("role") == "user" or (item.get("content") or "").strip())
    ]
    dom = pg.evaluate(
        "()=>[...document.querySelectorAll('[data-testid=transcript] .message-row[data-role]')].map(r=>[r.getAttribute('data-role'), r.getAttribute('data-message-id')||'', (r.textContent||'').slice(0,40)])"
    )
    roles_ok = [item[0] for item in stored] == [item[0] for item in dom]
    ids_ok = all((not left[1]) or (not right[1]) or left[1] == right[1] for left, right in zip(stored, dom)) and len(stored) == len(dom)
    return {
        "saved_n": len(stored),
        "dom_n": len(dom),
        "roles_equal": roles_ok,
        "ids_equal": ids_ok,
        "stale_status_rows": stale,
        "saved": [(item[0], item[2], item[3], item[4]) for item in stored],
        "dom_roles": [item[0] for item in dom],
        "msg_keys": sorted(messages[0].keys()) if messages else [],
    }


def _running(status):
    return (status or "").strip().startswith(("Writing", "Waiting", "Working", "Running", "Paused", "Thinking", "Queued"))


def _clock_seconds(status):
    text = status or ""
    minutes = re.search(r"(\d+)m (\d+)s", text)
    if minutes:
        return int(minutes.group(1)) * 60 + int(minutes.group(2))
    seconds = re.search(r"(\d+)s", text)
    if seconds:
        return int(seconds.group(1))
    return None


def problems_for(out):
    """Fail unless this case kept every row still and paused on time."""
    found = []
    if out.get("error"):
        found.append("error: " + str(out["error"])[:180])
        return found
    kind = out.get("kind")
    analysis = out.get("an") or {}
    for key, row in analysis.items():
        if not isinstance(row, dict) or "appear_ms" not in row:
            continue
        if row["appear_ms"] is None:
            found.append(key + " did not appear")
        if row["vanish_dup_events"]:
            found.append(key + " vanished or duplicated")
        if (row.get("max_step_px") or 0) > 2:
            found.append(key + f" jumped {row.get('max_step_px')}px")
    if (analysis.get("existing_rows_max_step_px") or 0) > 2:
        found.append(f"existing rows jumped {analysis.get('existing_rows_max_step_px')}px")
    if analysis.get("assistant_len_drops"):
        found.append(f"assistant text dropped {analysis.get('assistant_len_drops')} times")
    if analysis.get("row_reappear_events"):
        found.append(f"a row remounted {analysis.get('row_reappear_events')} times")
    if (analysis.get("max_frame_gap_ms") or 0) > 1000:
        found.append(f"frame gap {analysis.get('max_frame_gap_ms')}ms")
    if (analysis.get("finish_gap_px") or 0) > 2:
        found.append(f"finish jump {analysis.get('finish_gap_px')}px")
    saved_rows = (out.get("cmp") or {}).get("saved") or []
    assistants = [item for item in saved_rows if item[0] == "assistant"]
    long_replies = [item for item in assistants if str(item[1]).startswith("##")]
    if len(long_replies) > 1:
        found.append("reply duplicated")
    if long_replies and long_replies[0][2] and 3990 <= (long_replies[0][3] if len(long_replies[0]) > 3 else 0) <= 4100:
        found.append("paused a finished reply")
    partial = analysis.get("partial")
    if partial and kind != "after_end":
        if partial.get("frames_missing"):
            found.append("partial disappeared")
        minimum = partial.get("min_len_after")
        at_send = partial.get("len_at_send") or 0
        if minimum is not None and at_send and minimum < at_send - 20:
            found.append("partial shrank")
    # A reply that finished whole is not a pause, even if the send landed while the status still said Writing.
    first_interrupted = bool(assistants and assistants[0][2])
    busy = kind != "after_end" and _running(out.get("trigger_status")) and first_interrupted
    if kind == "tool" and not _running(out.get("trigger_status")):
        found.append("tool step was not showing before the send")
    if busy:
        pause = analysis.get("pause_ms")
        if pause is None or pause >= 200:
            found.append(f"pause was {pause}ms")
        if not out.get("paused_mark"):
            found.append("interrupted marker missing")
    if kind == "after_end":
        idle = out.get("first_idle_s")
        if idle is None or idle > 80:
            found.append(f"first reply idle {idle}s")
    if out.get("idle_s") is None:
        found.append("did not go idle")
    if out.get("nothing_in_flight_text"):
        found.append("wedge text on screen")
    for label in ("cmp", "cmp_final"):
        check = out.get(label) or {}
        if not check.get("roles_equal") or not check.get("ids_equal"):
            found.append(label + " order mismatched")
        blob = json.dumps(check.get("saved") or [])
        if "The model kept" in blob:
            found.append(label + " saved the runaway note over the reply")
    if kind == "reload":
        reload = out.get("reload") or {}
        status = reload.get("status") or ""
        if reload.get("visible_after_s") is None:
            found.append("partial did not return after reload")
        seconds = _clock_seconds(status)
        trigger = out.get("trigger_after_s") or 0
        if seconds is None or "m " in status or seconds + 8 < trigger:
            found.append(f"reload clock {status!r} is not the run start ({trigger}s in)")
    for follow in out.get("follow") or []:
        if follow.get("appear_ms") is None or follow.get("dup"):
            found.append("follow-up vanished or did not appear")
        if (follow.get("max_step_px") or 0) > 2 or (follow.get("existing_rows_max_step_px") or 0) > 2:
            found.append("follow-up moved an existing row")
        if follow.get("assistant_len_drops"):
            found.append("follow-up dropped assistant text")
        if follow.get("idle_s") is None:
            found.append("follow-up did not go idle")
    return found
