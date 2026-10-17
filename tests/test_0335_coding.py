"""The coding block tells the model to verify, and undo and spill keep the bytes."""

from pathlib import Path

from easyagent.codenote import remember_undo, spill_output
from easyagent.prompt import build_system


def test_the_coding_block_says_verify_undo_and_spill():
    text = build_system(bot_name="Ada", direction="", summary="", skills_text="")
    assert "Before you stop, verify" in text
    assert ".ea-undo" in text
    assert ".ea-spill" in text


def test_undo_keeps_the_previous_bytes_and_skips_a_secret(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("new", encoding="utf-8")
    saved = remember_undo(path, "old line\n")
    assert saved is not None
    assert saved.read_text(encoding="utf-8") == "old line\n"
    assert saved.parent == tmp_path / ".ea-undo"
    assert remember_undo(path, "-----BEGIN PRIVATE KEY-----\nabc\n") is None
    assert not (tmp_path / ".ea-undo" / "notes.txt").read_text(encoding="utf-8").startswith("-----BEGIN")


def test_a_long_result_spills_the_full_text(tmp_path):
    body = "START" + ("x" * 1000) + "END"
    dest = spill_output(body, tmp_path / ".ea-spill")
    assert dest is not None
    assert dest.read_text(encoding="utf-8") == body
    assert spill_output("sk_live_secretvalue", tmp_path / ".ea-spill", label="secret") is None
    assert not (tmp_path / ".ea-spill" / "secret.txt").exists()
