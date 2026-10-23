"""0.3.46 rc5: one routine card, no card when refused, and the card is on the stream."""

import json

from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.features import claim_routine_card, reset_routine_turn


def _world(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post("/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    return client, bot


def _spy(monkeypatch):
    import easyagent.safety as safety

    made = []
    real = safety._make_card

    def spy(bot_id, verdict, exact, proposal=None):
        card = real(bot_id, verdict, exact, proposal)
        made.append(card)
        return card

    monkeypatch.setattr(safety, "_make_card", spy)
    return made


class Script:
    def __init__(self, replies):
        self.replies = list(replies)
        self.n = 0

    async def __call__(self, **_kwargs):
        self.n += 1
        if self.n <= len(self.replies):
            return self.replies[self.n - 1]
        return "The pier is clear."


class Pieces:
    def __init__(self, text):
        self.text = text
        self.n = 0

    async def __call__(self, **_kwargs):
        self.n += 1
        yield self.text


def _fence(name, when, prompt, timezone=""):
    zone = f"timezone: {timezone}\n" if timezone else ""
    return (
        "```routine\n"
        "action: create\n"
        f"name: {name}\n"
        f"{when}\n"
        f"{zone}"
        "---\n"
        f"{prompt}\n"
        "```"
    )


def _send(client, bot, monkeypatch, text, replies):
    monkeypatch.setattr("easyagent.llm.complete", Script(replies))
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": text},
    )
    assert sent.status_code == 200, sent.text
    return sent.json()


def _routine_cards(made):
    return [card for card in made if card.rule == "routine"]


def test_one_request_raises_one_routine_card(tmp_path, monkeypatch):
    """A native routine proposal and the steer must not each raise a card.

    The same schedule and prompt, even under another name, is not a second card.
    """
    reset_routine_turn()
    assert claim_routine_card("Weekdays at 8:00 AM", "Check the site")
    assert not claim_routine_card("Weekdays at 8:00 AM", "Check the site")
    assert not claim_routine_card("  Weekdays at 8:00 AM  ", "Check\nthe site")
    assert not claim_routine_card("Daily at 8:00 AM", "check my site every morning at 8")
    reset_routine_turn()

    client, bot = _world(tmp_path)
    made = _spy(monkeypatch)
    morning = _fence("Site check", "weekdays: 8:00 AM", "Check the site")
    sent = _send(
        client,
        bot,
        monkeypatch,
        "check my site every morning at 8",
        [morning, "The site check is ready."],
    )
    raised = _routine_cards(made)
    assert len(raised) == 1, [card.proposal for card in raised]
    assert raised[0].proposal["name"] == "Site check"
    assert "Morning check" not in sent["reply"]
    pending = client.get(f"/api/bots/{bot['id']}/approvals").json()
    assert all((card.get("proposal") or {}).get("name") != "Morning check" for card in pending)

    made.clear()
    once = _fence("Kiln reminder", "once: 2026-04-01 09:00", "Wake the kiln", "America/Chicago")
    sent = _send(
        client,
        bot,
        monkeypatch,
        "remind me once to wake the kiln",
        [once, "The reminder is ready."],
    )
    raised = _routine_cards(made)
    assert len(raised) == 1, [card.proposal for card in raised]
    assert raised[0].proposal["name"] == "Kiln reminder"
    pending = client.get(f"/api/bots/{bot['id']}/approvals").json()
    assert all((card.get("proposal") or {}).get("name") != "Scheduled job" for card in pending)

    made.clear()
    again = _fence("Harbor check", "weekdays: 8:00 AM", "Check the site")
    _send(
        client,
        bot,
        monkeypatch,
        "check my site every morning at 8",
        [morning, again, "One card is enough."],
    )
    raised = _routine_cards(made)
    assert len(raised) == 1, [card.proposal for card in raised]
    assert raised[0].proposal["name"] == "Site check"
    assert raised[0].proposal["prompt"] == "Check the site"
    client.close()


def test_a_refused_routine_raises_no_card(tmp_path, monkeypatch):
    """A once-reminder at a time that does not exist raises no card, and the reply says why."""
    client, bot = _world(tmp_path)
    made = _spy(monkeypatch)
    missing = _fence("Spring alarm", "once: 2026-03-08 02:30", "Wake the kiln", "America/Chicago")
    sent = _send(
        client,
        bot,
        monkeypatch,
        "remind me once to wake the kiln",
        [missing, "I cannot set that."],
    )
    assert _routine_cards(made) == []
    assert client.get(f"/api/bots/{bot['id']}/approvals").json() == []
    reply = sent["reply"]
    assert "does not exist" in reply.lower()
    assert "confirm the routine card" not in reply.lower()
    client.close()


def test_a_routine_card_is_pushed_on_the_event_stream(tmp_path, monkeypatch):
    """A schedule question that ends in a plain answer still carries the card on the stream."""
    monkeypatch.setattr("easyagent.llm.stream_complete", Pieces("A routine can do that."))
    client, bot = _world(tmp_path)
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    url = f"/api/bots/{bot['id']}/chats/{chat['id']}/messages"
    with client.stream(
        "POST",
        url,
        json={"content": "how about cron jobs?"},
        headers={"Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, response.read()
        body = response.read().decode()
    events = []
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        events.append(json.loads(line.split(":", 1)[1].strip()))
    dones = [event for event in events if event.get("type") == "done"]
    assert dones, [event.get("type") for event in events]
    cards = dones[-1].get("approvals") or []
    assert len(cards) == 1
    assert cards[0]["rule"] == "routine"
    assert cards[0]["proposal"]["kind"] == "routine"
    pending = client.get(f"/api/bots/{bot['id']}/approvals").json()
    assert len(pending) == 1
    assert pending[0]["id"] == cards[0]["id"]
    assert pending[0]["rule"] == "routine"
    client.close()
