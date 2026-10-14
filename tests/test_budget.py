"""The tool budget belongs to one run, and it counts tool time only."""

import asyncio

from easyagent import safety
from easyagent import turn as turn_mod
from easyagent.check import _grade
from easyagent.judge import grade_text
from easyagent.store import Store
from easyagent.tools import ToolError, ToolRequest, execute
from easyagent.workspace import assign_workspace

INDEX = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Weeknight recipes</title></head>
<body>
<h1>Weeknight recipes</h1>
<p>Three pages for a quiet evening.</p>
<ul>
<li><a href="tomato-soup.html">Tomato soup</a></li>
<li><a href="brown-bread.html">Brown bread</a></li>
</ul>
</body>
</html>
"""

SOUP = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Tomato soup</title></head>
<body>
<h1>Tomato soup</h1>
<p>Simmer tomatoes, onion, and a pinch of salt. Blend until smooth.</p>
<p><a href="index.html">Back to the recipes</a></p>
</body>
</html>
"""

BREAD = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Brown bread</title></head>
<body>
<h1>Brown bread</h1>
<p>Mix wholemeal flour, warm water, and yeast. Let it rise, then bake.</p>
<p><a href="index.html">Back to the recipes</a></p>
</body>
</html>
"""

PANTRY = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Pantry</title></head>
<body>
<h1>Pantry</h1>
<p>Tomatoes, onion, wholemeal flour, yeast, and salt.</p>
</body>
</html>
"""

MENU = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Tonight</title></head>
<body>
<h1>Tonight</h1>
<p>Soup first, then bread. Both pages are linked from the home page.</p>
</body>
</html>
"""


def _world(tmp_path):
    store = Store(tmp_path / "data")
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key=None, model="bot")
    bot = store.add_bot(name="Cook", endpoint_id=endpoint["id"], model="bot")
    assign_workspace(store, bot, str(tmp_path / "site"))
    chat = store.create_chat(bot["id"])
    return store, bot, chat


def _clock(monkeypatch):
    state = {"t": 1_000.0}
    monkeypatch.setattr(safety, "_now", lambda: state["t"])
    return state


def _write(name: str, body: str) -> ToolRequest:
    return ToolRequest(kind="files", action="write", path=name, body=body)


def test_a_second_turn_builds_the_recipe_site_after_twenty_minutes(tmp_path, monkeypatch):
    store, bot, chat = _world(tmp_path)
    clock = _clock(monkeypatch)
    site = tmp_path / "site"

    async def run():
        turn_mod.bind(store, chat["id"], bot_id=bot["id"])
        first = await execute(store, _write("index.html", INDEX), bot["id"])
        clock["t"] += 21 * 60
        turn_mod.bind(store, chat["id"], bot_id=bot["id"])
        second = await execute(store, _write("tomato-soup.html", SOUP), bot["id"])
        third = await execute(store, _write("brown-bread.html", BREAD), bot["id"])
        return first, second, third

    first, second, third = asyncio.run(run())
    for text in (first, second, third):
        assert "twenty minutes" not in text
        assert "It was not run." not in text
    assert "Weeknight recipes" in (site / "index.html").read_text(encoding="utf-8")
    assert "Tomato soup" in (site / "tomato-soup.html").read_text(encoding="utf-8")
    assert "Brown bread" in (site / "brown-bread.html").read_text(encoding="utf-8")
    assert 'href="tomato-soup.html"' in (site / "index.html").read_text(encoding="utf-8")


def test_a_slow_model_does_not_spend_the_tool_budget(tmp_path, monkeypatch):
    store, bot, chat = _world(tmp_path)
    clock = _clock(monkeypatch)
    site = tmp_path / "site"

    async def run():
        turn_mod.bind(store, chat["id"], bot_id=bot["id"])
        pages = (
            ("index.html", INDEX),
            ("tomato-soup.html", SOUP),
            ("brown-bread.html", BREAD),
            ("pantry.html", PANTRY),
            ("menu.html", MENU),
            ("evening.html", "<!DOCTYPE html><html><body><h1>Evening</h1><p>The pages are ready.</p></body></html>"),
        )
        await execute(store, _write(*pages[0]), bot["id"])
        for name, body in pages[1:]:
            clock["t"] += 5 * 60
            text = await execute(store, _write(name, body), bot["id"])
            assert "twenty minutes" not in text
            assert "It was not run." not in text
        return turn_mod.current_slot()

    slot = asyncio.run(run())
    assert clock["t"] - 1_000.0 > safety.MAX_SECONDS
    assert slot.safety_budget["seconds"] < 1
    assert "Weeknight recipes" in (site / "index.html").read_text(encoding="utf-8")
    assert "Simmer tomatoes" in (site / "tomato-soup.html").read_text(encoding="utf-8")
    assert "wholemeal flour" in (site / "brown-bread.html").read_text(encoding="utf-8")


def test_twenty_minutes_of_running_tools_stops_the_next_call(tmp_path, monkeypatch):
    store, bot, chat = _world(tmp_path)
    clock = _clock(monkeypatch)
    from easyagent import tools as tools_mod

    real_execute = tools_mod._execute

    def slow(store, request, bot_id=None):
        if request.path == "brown-bread.html":
            clock["t"] += 21 * 60
        return real_execute(store, request, bot_id)

    monkeypatch.setattr(tools_mod, "_execute", slow)

    async def run():
        turn_mod.bind(store, chat["id"], bot_id=bot["id"])
        await execute(store, _write("index.html", INDEX), bot["id"])
        await execute(store, _write("brown-bread.html", BREAD), bot["id"])
        await execute(store, _write("tomato-soup.html", SOUP), bot["id"])

    try:
        asyncio.run(run())
    except ToolError as exc:
        assert "twenty minutes of tool time" in str(exc)
        assert "It was not run." in str(exc)
    else:
        raise AssertionError("the tool after twenty minutes of tool time was allowed")
    assert (tmp_path / "site" / "brown-bread.html").is_file()
    assert not (tmp_path / "site" / "tomato-soup.html").is_file()


def test_a_new_turn_resets_the_call_cap(tmp_path, monkeypatch):
    store, bot, chat = _world(tmp_path)
    _clock(monkeypatch)

    async def run():
        turn_mod.bind(store, chat["id"], bot_id=bot["id"])
        listing = ToolRequest(kind="files", action="list", path=".")
        for _ in range(safety.MAX_CALLS):
            await execute(store, listing, bot["id"])
        with_cap = None
        try:
            await execute(store, listing, bot["id"])
        except ToolError as exc:
            with_cap = str(exc)
        turn_mod.bind(store, chat["id"], bot_id=bot["id"])
        again = await execute(store, listing, bot["id"])
        return with_cap, again

    blocked, again = asyncio.run(run())
    assert blocked is not None
    assert f"{safety.MAX_CALLS} tools" in blocked
    assert "It was not run." in blocked
    assert "It was not run." not in again


def test_a_local_check_and_grader_wait_ten_minutes(monkeypatch):
    from easyagent import llm
    from easyagent import retry

    assert llm.grade_timeout("http://127.0.0.1:9/v1") == llm.LOCAL_GRADE_TIMEOUT
    monkeypatch.setattr("easyagent.access.is_lan", lambda host: host == "oven.example")
    assert llm.grade_timeout("http://oven.example:8080/v1") == llm.LOCAL_GRADE_TIMEOUT
    assert llm.grade_timeout("https://api.example.com/v1") == 120.0
    assert llm.LOCAL_GRADE_TIMEOUT >= 600
    long = retry.Window(budget=120, long_wait=True)
    assert long.attempt_limit(llm.LOCAL_GRADE_TIMEOUT) == llm.LOCAL_GRADE_TIMEOUT
    chat = retry.Window(budget=120)
    assert chat.attempt_limit(llm.LOCAL_GRADE_TIMEOUT) == 120

    seen = []

    async def complete(**kwargs):
        seen.append(kwargs)
        return '{"pass": true, "problems": [], "fix_hint": ""}'

    monkeypatch.setattr(llm, "complete", complete)

    async def both():
        await _grade(
            base_url="http://127.0.0.1:9/v1",
            api_key=None,
            model="bot",
            request="Build the recipe pages.",
            reply="The three pages are written.",
            tool_text="files write ok",
        )
        await grade_text(
            endpoint={"id": "e", "name": "home", "base_url": "https://api.example.com/v1", "model": "bot"},
            bot={"name": "Cook", "model": "bot"},
            rubric="The pages exist.",
            request="Build the recipe pages.",
            reply="Done.",
            tool_text="",
        )

    asyncio.run(both())
    local, public = seen
    assert local["timeout"] == llm.LOCAL_GRADE_TIMEOUT
    assert local["long_wait"] is True
    assert public["timeout"] == 120.0
    assert public["long_wait"] is True
