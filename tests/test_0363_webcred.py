"""Wave 2: a saved web credential is typed by name. The secret never enters the chat.

The model types ``vault:<name>``; the harness opens ``web|<host>|<name>`` at execution,
against the live page's host only, after the person approves a card that shows just
the credential name and the host.
"""

import asyncio
import json
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

os.environ["EASYAGENT_BROWSER_HEADLESS"] = "1"

from easyagent.browser import _HUB, _page, judge_browser, reset_for_tests, vault_ref
from easyagent.safety import list_pending, resolve_card
from easyagent.secrets import (
    get_web_credential,
    list_web_credentials,
    put_web_credential,
    web_credential_key,
)
from easyagent.store import Store
from easyagent.tools import ToolError, ToolRequest, execute

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "browser"
SECRET = "ada-s3cret-passphrase-9182"


@pytest.fixture(autouse=True)
def _close_browsers():
    yield
    reset_for_tests()


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return


def _serve() -> ThreadingHTTPServer:
    handler = partial(_Quiet, directory=str(FIXTURE))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _bot(tmp_path: Path):
    store = Store(tmp_path)
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    return store, bot


def _request(action: str, url: str, **meta) -> ToolRequest:
    payload = {"url": url, "element": "", "values": "", "secret": False, "rule": ""}
    payload.update(meta)
    return ToolRequest(
        kind="browser",
        action=action,
        path=url,
        body=meta.get("body") or "",
        call_arguments=json.dumps(payload),
    )


def _number(snapshot_text: str, label: str) -> int:
    for line in snapshot_text.splitlines():
        if label in line and line.strip().startswith("["):
            return int(line.split("]", 1)[0].strip("["))
    raise AssertionError(label + "\n" + snapshot_text)


async def _wait_card(bot_id: str) -> dict:
    for _ in range(80):
        rows = list_pending(bot_id)
        if rows:
            return rows[0]
        await asyncio.sleep(0.05)
    raise AssertionError("no approval card")


def _field_value(bot_id: str, selector: str) -> str:
    """Test-only probe: read the live field straight from the page."""

    def work(playwright, sessions):
        return _page(sessions[bot_id]).input_value(selector)

    return _HUB.call(work)


# (d) key format and the names-only list function


def test_web_credential_key_format_and_list(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    store = Store(tmp_path)
    store.ensure()
    assert web_credential_key("GitHub.COM", " ada ") == "web|github.com|ada"
    put_web_credential("github.com", "ada", SECRET, store)
    put_web_credential("127.0.0.1", "work", "other-value-7", store)
    put_web_credential("GITHUB.com", "ada", SECRET, store)  # same key, lowercased
    rows = list_web_credentials(store)
    assert {"host": "github.com", "name": "ada"} in rows
    assert {"host": "127.0.0.1", "name": "work"} in rows
    assert len(rows) == len({(r["host"], r["name"]) for r in rows})
    blob = json.dumps(rows)
    assert SECRET not in blob and "other-value-7" not in blob  # names and hosts only
    assert get_web_credential("github.com", "ada", store) == SECRET
    assert get_web_credential("evil.com", "ada", store) == ""


def test_scrubbing_covers_web_credential_values(tmp_path, monkeypatch):
    """secret_values opens every account, so web|host|name values are scrubbed with."""
    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    store = Store(tmp_path)
    store.ensure()
    put_web_credential("github.com", "ada", SECRET, store)
    from easyagent.journal import scrub_text
    from easyagent.tools import secret_strings

    assert SECRET in secret_strings(store)
    assert SECRET not in scrub_text(store, f"the password was {SECRET}!")


# (unit) the judge asks on a vault: token and shows only name + host


def test_judge_asks_for_a_vault_token_by_name():
    request = _request(
        "type",
        "https://github.com/login",
        element='[1] textbox "Password" password',
        body="vault:ada",
        rule="browser-credential",
    )
    verdict = judge_browser(request)
    assert verdict.tier == "ask"
    assert verdict.rule == "browser-credential"
    assert "ada" in verdict.why and "github.com" in verdict.why
    assert SECRET not in verdict.why and SECRET not in verdict.detail
    plain = _request("type", "https://github.com/login", element='[2] textbox "Note"', body="vault: ada is not a ref")
    assert judge_browser(plain).tier == "allow"
    assert vault_ref("  vault:ada  ") == "ada"
    assert vault_ref("vault:") == ""
    assert vault_ref("my vault: ada") == ""


# (a)+(b)+(c) end to end on a fixture page served from 127.0.0.1


def test_vault_fill_matches_host_card_shows_name_only(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "8")
    server = _serve()
    port = server.server_address[1]
    store, bot = _bot(tmp_path)
    chat = store.create_chat(bot["id"])
    put_web_credential("127.0.0.1", "ada", SECRET, store)
    base = f"http://127.0.0.1:{port}"

    async def go(action: str, **fields):
        request = ToolRequest(
            kind="browser",
            action=action,
            path=fields.get("path", ""),
            command=fields.get("command", ""),
            body=fields.get("body", ""),
        )
        return await execute(store, request, bot["id"])

    async def once():
        await go("open", path=f"{base}/login.html")
        page = await go("read")
        password = _number(page, "Password")

        typing = asyncio.create_task(go("type", command=str(password), body="vault:ada"))
        card = await _wait_card(bot["id"])
        blob = json.dumps(card)
        assert card["rule"] == "browser-credential"
        assert card["proposal"]["kind"] == "credential"
        assert card["proposal"]["name"] == "ada"
        assert card["proposal"]["host"] == "127.0.0.1"
        assert "ada" in blob and "127.0.0.1" in blob
        assert SECRET not in blob  # the card never shows the secret
        resolve_card(card["id"], "approve")
        result = await typing
        assert "typed" in result.lower()
        assert SECRET not in result
        assert _field_value(bot["id"], "#password") == SECRET  # filled for real

        # The transcript rows never carry the secret.
        live = store.get_chat(bot["id"], chat["id"])
        live["messages"] = list(live.get("messages") or []) + [
            {"id": "11111111-1111-1111-1111-111111111111", "role": "tool", "content": result},
            {"id": "33333333-3333-3333-3333-333333333333", "role": "assistant", "content": "Typed vault:ada into the password box."},
        ]
        store.save_chat(live)
        stored = json.dumps(store.get_chat(bot["id"], chat["id"]))
        assert SECRET not in stored
        reread = await go("read")
        assert SECRET not in reread  # the snapshot masks the password field

    try:
        asyncio.run(once())
    finally:
        server.shutdown()


def test_vault_fill_wrong_host_types_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYAGENT_KEYRING", "memory")
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "8")
    server = _serve()
    port = server.server_address[1]
    store, bot = _bot(tmp_path)
    put_web_credential("example.org", "ada", SECRET, store)  # not this host
    base = f"http://127.0.0.1:{port}"

    async def go(action: str, **fields):
        request = ToolRequest(
            kind="browser",
            action=action,
            path=fields.get("path", ""),
            command=fields.get("command", ""),
            body=fields.get("body", ""),
        )
        return await execute(store, request, bot["id"])

    async def once():
        await go("open", path=f"{base}/login.html")
        page = await go("read")
        password = _number(page, "Password")
        typing = asyncio.create_task(go("type", command=str(password), body="vault:ada"))
        card = await _wait_card(bot["id"])
        assert card["rule"] == "browser-credential"
        resolve_card(card["id"], "approve")
        with pytest.raises(ToolError) as raised:
            await typing
        message = str(raised.value)
        assert "ada" in message and "127.0.0.1" in message
        assert SECRET not in message  # names what is missing, never the value
        assert _field_value(bot["id"], "#password") == ""  # nothing was typed

    try:
        asyncio.run(once())
    finally:
        server.shutdown()
