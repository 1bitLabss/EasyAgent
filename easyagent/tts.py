"""Server-side speech for this bot, through the free Microsoft Edge voices.

The browser's own speechSynthesis sounds robotic and is silent in webviews
that do not ship it, so speech is synthesized here instead. edge-tts needs
the internet; nothing is stored and no key is required. Each bot keeps a
voice preference beside its other settings.
"""

from __future__ import annotations

import asyncio
import threading

from easyagent.store import Store, atomic_write_json

DEFAULT_VOICE = "en-US-AriaNeural"
MAX_CHARS = 4000
DEFAULT_TIMEOUT = 30.0

# A small picker list: common neural voices across genders and accents.
VOICES = [
    {"id": "en-US-AriaNeural", "label": "Aria (English, US — female)"},
    {"id": "en-US-GuyNeural", "label": "Guy (English, US — male)"},
    {"id": "en-US-JennyNeural", "label": "Jenny (English, US — female)"},
    {"id": "en-GB-SoniaNeural", "label": "Sonia (English, UK — female)"},
    {"id": "en-GB-RyanNeural", "label": "Ryan (English, UK — male)"},
    {"id": "en-AU-NatashaNeural", "label": "Natasha (English, Australia — female)"},
    {"id": "en-AU-WilliamNeural", "label": "William (English, Australia — male)"},
    {"id": "en-IN-NeerjaNeural", "label": "Neerja (English, India — female)"},
]

_VOICE_IDS = {voice["id"] for voice in VOICES}


def is_known_voice(voice: str) -> bool:
    return (voice or "") in _VOICE_IDS


def resolve_voice(voice: str | None) -> str:
    """The voice to speak with. An empty choice means the default."""
    voice = (voice or "").strip()
    return voice or DEFAULT_VOICE


def get_voice(bot: dict | None) -> str:
    """This bot's saved voice, or the default when it has none."""
    voice = ((bot or {}).get("tts_voice") or "").strip()
    return voice if is_known_voice(voice) else DEFAULT_VOICE


def save_voice(store: Store, bot_id: str, voice: str) -> dict:
    """This bot's voice preference. Chats are not rewritten."""
    if not is_known_voice(voice):
        raise ValueError("That voice is not on the list.")
    with store._lock:
        bot = store.get_bot(bot_id)
        bot["tts_voice"] = voice
        atomic_write_json(store._bot_dir(bot["id"]) / "bot.json", bot)
        return bot


async def _amass(text: str, voice: str) -> bytes:
    """Pull the whole mp3 out of edge-tts's audio stream."""
    import edge_tts

    buffer = bytearray()
    communicate = edge_tts.Communicate(text, voice)
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio" and chunk.get("data"):
            buffer.extend(chunk["data"])
    if not buffer:
        raise RuntimeError("The voice service returned no audio.")
    return bytes(buffer)


def synthesize(text: str, voice: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """Speak text with voice and return mp3 bytes.

    edge-tts is asyncio and this code is sync, so the run happens on a
    short-lived thread with a hard timeout: a hung socket cannot hold the
    caller forever. Any failure comes back as a plain sentence.
    """
    from easyagent.tools import ToolError

    text = (text or "").strip()
    if not text:
        raise ToolError("There is nothing to say.")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
    voice = resolve_voice(voice)

    outcome: dict = {}

    def _run() -> None:
        try:
            outcome["data"] = asyncio.run(_amass(text, voice))
        except Exception as exc:  # noqa: BLE001 - the caller gets one sentence
            outcome["error"] = exc

    worker = threading.Thread(target=_run, name="easyagent-tts", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise ToolError("Speech took too long to come back. Try a shorter line.")
    if "error" in outcome:
        raise ToolError("Speech did not come back. Check the internet connection and try again.")
    return outcome["data"]
