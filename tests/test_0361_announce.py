"""A reply that only announces the work must not end the turn.

The screenshot bug: the agent researched mosques, then ended the turn on
"I have enough to build the first version. Writing it into the Save America
vault now." and no file was ever written. Present-participle promises
("Writing it now", "Doing it now") carried no lead phrase, so the old
announcement detector let them close the turn as a finished answer.
"""

from easyagent import turn as turn_mod
from easyagent.features import reset_routine_turn

ANNOUNCED = (
    "I have enough to build the first version. "
    "Writing it into the Save America vault now."
)


def fresh_turn_context() -> None:
    """Start the way a real turn starts. A direct call binds no turn slot, so
    without this it inherits a stop flag or routine-card state an earlier
    test file left in this thread's context."""
    turn_mod.renew_cancel()
    reset_routine_turn()


def _world(tmp_path, monkeypatch, reply="ack"):
    """Fake model at the llm.complete seam, same tape pattern as test_check."""
    from fastapi.testclient import TestClient

    from easyagent.app import create_app

    class Recorder:
        def __init__(self, reply):
            self.reply = reply
            self.seen = []

        async def __call__(self, *, base_url, api_key, model, messages, timeout=120):
            self.seen.append(messages)
            reply = self.reply
            if isinstance(reply, list):
                reply = reply.pop(0) if reply else "ack"
            return reply(messages) if callable(reply) else reply

    recorder = Recorder(reply)
    monkeypatch.setattr("easyagent.llm.complete", recorder)
    app = create_app(tmp_path)
    client = TestClient(app)
    endpoint = client.post(
        "/api/endpoints", json={"name": "local", "base_url": "http://127.0.0.1:9/v1"}
    ).json()
    bot = client.post("/api/bots", json={"name": "Ada", "endpoint_id": endpoint["id"]}).json()
    chat = client.post(f"/api/bots/{bot['id']}/chats").json()
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "keep-this-line"},
    )
    assert sent.status_code == 200, sent.text
    return client, bot, chat, recorder


def test_the_screenshot_line_is_an_announcement():
    from easyagent.tools import _is_announcement

    assert _is_announcement(ANNOUNCED)
    assert _is_announcement("Doing it now.")
    assert _is_announcement("Let me pull the actual listings to get mosque names.")
    assert _is_announcement("Pulling the listings now.")
    assert _is_announcement("Now writing it into the vault.")


def test_finished_results_and_generalities_are_not_announcements():
    from easyagent.tools import _is_announcement

    # Past tense is a result, not a promise.
    assert not _is_announcement("I wrote it into the vault.")
    # A question for the person is the reply.
    assert not _is_announcement("Should I write it to the vault?")
    # A gerund-subject generalization is not a step this turn owes.
    assert not _is_announcement("Writing tests is a good habit.")
    # A standing offer is a greeting.
    assert not _is_announcement("I'll help you look things up.")
    # "Ready to help" with no named work is a greeting, not a step owed.
    assert not _is_announcement("Hi! I am ready to help.")
    assert not _is_announcement("I am ready to help anytime!")
    # But "I have enough to build it" names the owed step and does fire.
    assert _is_announcement("I have enough to build the first version.")
    # A finished answer that mentions a later check stays a finished answer.
    assert not _is_announcement(
        "The kiln cooled overnight and the glaze is even across the whole shelf. "
        "The batch is usable as it stands. I will check the next firing later."
    )
    # A tool fence is never an announcement.
    assert not _is_announcement("```search\nmosque names near me\n```")
    assert not _is_announcement("Let me check the list.\n```search\nmosque names\n```")


def test_the_turn_does_not_end_on_the_announcement(tmp_path, monkeypatch):
    """The loop must keep going past the promise, not seal it as the answer."""
    fresh_turn_context()
    client, bot, chat, recorder = _world(tmp_path, monkeypatch)
    before = len(recorder.seen)
    recorder.reply = [
        ANNOUNCED,
        "Done. The mosque list is in the vault file.",
    ]
    sent = client.post(
        f"/api/bots/{bot['id']}/chats/{chat['id']}/messages",
        json={"content": "Write the mosque list into the Save America vault file."},
    )
    assert sent.status_code == 200, sent.text
    answer = sent.json()["chat"]["messages"][-1]["content"]
    # The turn continued: the model was asked again after the announcement.
    assert len(recorder.seen) >= before + 2
    # The promise was not sealed as the finished reply.
    assert "Writing it into the Save America vault now." not in answer
    assert answer != ANNOUNCED
