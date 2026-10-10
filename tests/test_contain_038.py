"""0.3.8 OS containment. Windows is monkeypatched. Linux runs the real container."""

import asyncio
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from easyagent.app import create_app
from easyagent.contain import (
    SECURITY_CAPABILITIES,
    blocked_acl_target,
    consented,
    grant_targets,
    next_mechanism,
    setup,
    undo,
)
from easyagent.safety import list_pending, resolve_card
from easyagent.sandbox import macos_profile, public_status, shell_mode
from easyagent.store import Store


def _bot(tmp_path: Path):
    store = Store(tmp_path / "data")
    store.ensure()
    endpoint = store.add_endpoint(name="local", base_url="http://127.0.0.1:9/v1", api_key="")
    bot = store.add_bot(name="Ada", endpoint_id=endpoint["id"], model=None)
    return store, bot


def test_the_container_attribute_is_security_capabilities(monkeypatch):
    from easyagent import contain

    assert SECURITY_CAPABILITIES == 0x00020009
    assert SECURITY_CAPABILITIES != 0x00020000
    seen = {}

    def fake(_kernel32, _argv, _env, _cwd, attribute, _value_ptr, _value_size):
        seen["attribute"] = attribute

        class Proc:
            def communicate(self, timeout=None):
                return "", ""

        return Proc()

    monkeypatch.setattr(contain, "_create_with_attribute", fake)
    contain._create_appcontainer(None, ["whoami.exe"], {}, ".", 1, False)
    assert seen["attribute"] == 0x00020009


def test_a_failed_appcontainer_uses_the_restricted_token():
    assert next_mechanism(None, True) == "appcontainer"
    assert next_mechanism("UpdateProcThreadAttribute failed (Win32 24)", True) == "restricted-token"
    with pytest.raises(OSError, match="not run"):
        next_mechanism("UpdateProcThreadAttribute failed (Win32 24)", False)


def test_grants_never_name_the_keychain(tmp_path):
    store, bot = _bot(tmp_path)
    assert blocked_acl_target(Path(r"C:\Users\me\AppData\Local\Microsoft\Credentials"))
    assert blocked_acl_target(Path(r"C:\Users\me\AppData\Local\Microsoft\Vault"))
    assert blocked_acl_target(Path(r"C:\Users\me\AppData\Roaming\Microsoft\Protect"))
    assert blocked_acl_target(Path.home() / ".easyagent")
    targets = grant_targets(store, bot["id"], extra=[Path(r"C:\Users\me\AppData\Local\Microsoft\Credentials")])
    blob = " ".join(str(path) for path, _rights in targets).lower()
    assert "credentials" not in blob
    assert "vault" not in blob
    assert "protect" not in blob
    assert ".easyagent" not in blob
    rights = {rights for _path, rights in targets}
    assert "(OI)(CI)M" in rights
    assert str(store.root) not in {str(path) for path, rights in targets if rights != "(X)"}


def test_setup_and_undo_do_not_touch_acls_off_windows(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("Windows setup creates the EasyAgent.Bot profile; this case is the other platforms")
    store, bot = _bot(tmp_path)

    def refuse(*_args, **_kwargs):
        raise AssertionError("setup called a subprocess")

    monkeypatch.setattr(subprocess, "run", refuse)
    setup(store, bot["id"])
    assert consented()
    assert public_status(store, bot["id"])["consented"] is True
    undo(store, bot["id"])
    assert not consented()
    assert public_status(store, bot["id"])["status"] == "off"


def test_the_consent_card_explains_the_setup(tmp_path, monkeypatch):
    from easyagent import contain

    calls = []

    def fake_setup(store=None, bot_id=None, *, allow=None):
        calls.append(bot_id)
        contain.save_ledger({"consented": True, "profile": "", "sid": "", "aces": [], "platform": os.name})
        return contain.load_ledger()

    def refuse_profile(*_args, **_kwargs):
        raise AssertionError("pytest created an EasyAgent.Bot profile")

    monkeypatch.setattr(contain, "setup", fake_setup)
    monkeypatch.setattr(contain, "_container_sid", refuse_profile)
    store, bot = _bot(tmp_path)
    monkeypatch.setenv("EASYAGENT_APPROVAL_SECONDS", "30")

    async def deny():
        task = asyncio.create_task(contain.ensure_consent(store, bot["id"]))
        for _ in range(20):
            pending = list_pending(bot["id"])
            if pending:
                break
            await asyncio.sleep(0.01)
        assert pending
        assert "AppContainer" in pending[0]["why"]
        assert "contain --undo" in pending[0]["why"]
        resolve_card(pending[0]["id"], "deny")
        with pytest.raises(Exception):
            await task

    asyncio.run(deny())
    assert not consented()

    async def allow():
        task = asyncio.create_task(contain.ensure_consent(store, bot["id"]))
        for _ in range(20):
            pending = list_pending(bot["id"])
            if pending:
                break
            await asyncio.sleep(0.01)
        resolve_card(pending[0]["id"], "approve")
        await task

    asyncio.run(allow())
    assert consented()
    assert calls == [bot["id"]]


def test_sandbox_is_off_until_consent(tmp_path):
    store, bot = _bot(tmp_path)
    client = TestClient(create_app(store.root))
    body = client.get("/api/sandbox").json()
    assert body["status"] == "off"
    assert body["active"] is False
    per_bot = client.get(f"/api/bots/{bot['id']}/sandbox").json()
    assert per_bot["label"] == "off"


def test_shell_stays_on_the_file_guards_until_consent(tmp_path, monkeypatch):
    from easyagent import turn as turn_mod
    from easyagent.tools import _run_shell

    store, bot = _bot(tmp_path)
    seen = {}

    class Proc:
        returncode = 0

        def communicate(self, timeout=None):
            return "plain-ok", ""

    def fake_popen(args, **kwargs):
        seen["args"] = args
        seen["shell"] = kwargs.get("shell")
        return Proc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    slot = turn_mod.slot_for(store, "chat-plain")
    slot.bot_id = bot["id"]
    slot.store = store
    slot.cwd = str(tmp_path)
    turn_mod._slot.set(slot)
    try:
        heard = _run_shell(store, "echo plain-ok")
    finally:
        turn_mod._slot.set(None)
    assert heard == "plain-ok"
    if os.name == "nt":
        assert seen["shell"] is False
        assert seen["args"][0] == "powershell.exe"
    else:
        assert seen["shell"] is True
    assert "bwrap" not in str(seen["args"])


def test_a_failed_probe_keeps_the_file_guards_and_says_so(tmp_path, monkeypatch):
    from easyagent import selftest
    from easyagent import turn as turn_mod
    from easyagent.tools import _run_shell

    store, bot = _bot(tmp_path)
    from easyagent.contain import save_ledger

    save_ledger({"consented": True, "profile": "", "sid": "", "aces": [], "platform": "test"})
    monkeypatch.setattr(
        selftest,
        "cached_probe",
        lambda: {"passed": False, "reason": "probe failed", "mechanism": "none", "status": "unavailable", "rows": []},
    )

    class Proc:
        returncode = 0

        def communicate(self, timeout=None):
            return "still-ran", ""

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Proc())
    slot = turn_mod.slot_for(store, "chat-fallback")
    slot.bot_id = bot["id"]
    slot.store = store
    slot.cwd = str(tmp_path)
    turn_mod._slot.set(slot)
    try:
        heard = _run_shell(store, "echo still-ran")
    finally:
        turn_mod._slot.set(None)
    assert heard.startswith("OS containment is unavailable:")
    assert "file guards" in heard
    assert "still-ran" in heard
    assert shell_mode(store)["label"].startswith("unavailable:")
    assert public_status(store, bot["id"])["active"] is False


def test_macos_profile_denies_the_data_dir_and_keychain():
    data = Path("/tmp/easyagent-data")
    work = data / "bots" / "ada" / "workspace"
    text = macos_profile([work], data)
    assert str(data) in text
    assert "securityd" in text
    assert "Keychains" in text or ".easyagent" in text
    assert str(work) in text


def test_landlock_holds_when_bubblewrap_is_missing(monkeypatch):
    import shutil

    from easyagent.selftest import _linux_rows

    if os.name == "nt":
        pytest.skip("Landlock is a Linux fallback")
    real = shutil.which

    def which(name, *args, **kwargs):
        if name == "bwrap":
            return None
        return real(name, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", which)
    rows = _linux_rows()
    assert any(name == "mechanism" and detail == "landlock" and status == "PASS" for name, status, detail in rows), rows
    assert rows[-1][1] == "PASS", rows


def test_the_real_container_hides_the_data_dir(tmp_path):
    from easyagent import turn as turn_mod
    from easyagent.selftest import _linux_rows
    from easyagent.tools import _run_shell

    if os.name == "nt":
        pytest.skip("the Windows probe runs on the laptop")
    rows = _linux_rows()
    table = {name: (status, detail) for name, status, detail in rows}
    assert table["RESULT"][0] == "PASS", rows
    for name in ("data-read", "secrets", "workspace-write", "web"):
        assert table[name][0] == "PASS", rows
    store, bot = _bot(tmp_path)
    workspace = store.root / "bots" / bot["id"] / "workspace"
    workspace.mkdir(parents=True)
    token = "CANARY-workspace-hidden"
    (store.root / "endpoints.json").write_text(token, encoding="utf-8")
    setup(store, bot["id"])
    from easyagent import selftest

    selftest.reset_probe()
    slot = turn_mod.slot_for(store, "chat-contained")
    slot.bot_id = bot["id"]
    slot.store = store
    slot.cwd = str(workspace)
    turn_mod._slot.set(slot)
    try:
        status = public_status(store, bot["id"])
        assert status["status"] == "active", status
        assert status["active"] is True
        heard = _run_shell(store, "echo contained-ok")
        assert "contained-ok" in heard
        with pytest.raises(Exception, match="saved chats|not run"):
            _run_shell(store, f"cat {store.root / 'endpoints.json'}")
        printed = _run_shell(store, "python3 -c 'print(1+1)'")
        assert "2" in printed
    finally:
        turn_mod._slot.set(None)
        undo(store, bot["id"])


class _WinFn:
    def __init__(self, name, hr=0, log=None):
        self.name = name
        self.hr = hr
        self.log = log
        self.restype = None
        self.argtypes = None

    def __call__(self, *args):
        if self.log is not None:
            self.log.append(self.name)
        if self.name == "DeriveAppContainerSidFromAppContainerName":
            args[1]._obj.value = 0x11
            return 0
        if self.name == "CreateAppContainerProfile":
            return self.hr
        if self.name == "ConvertStringSidToSidW":
            args[1]._obj.value = 0x21
            return 1
        if self.name == "DeleteAppContainerProfile":
            return 0
        if self.name == "GetCurrentProcess":
            return -1
        if self.name == "OpenProcessToken":
            return 0
        return 1


class _WinDll:
    def __init__(self, log, hr=0):
        self.log = log
        self.hr = hr

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        fn = _WinFn(name, self.hr, self.log)
        setattr(self, name, fn)
        return fn


def test_every_win32_call_declares_restype_and_argtypes():
    import re
    from ctypes import wintypes

    from easyagent import contain

    source = Path(contain.__file__).read_text(encoding="utf-8")
    used = re.findall(r"(kernel32|advapi|userenv)\.([A-Za-z_][A-Za-z0-9_]*)", source)
    for library, pattern in (
        ("kernel32", r'_dll\("kernel32"\)\.([A-Za-z_][A-Za-z0-9_]*)'),
        ("advapi", r'_dll\("advapi32"\)\.([A-Za-z_][A-Za-z0-9_]*)'),
        ("userenv", r'_dll\("userenv"\)\.([A-Za-z_][A-Za-z0-9_]*)'),
    ):
        used.extend((library, name) for name in re.findall(pattern, source))
    assert used
    libs = {"kernel32": "kernel32", "advapi": "advapi32", "userenv": "userenv"}
    sigs = contain.win32_signatures()
    missing = []
    for alias, name in used:
        spec = sigs.get(libs[alias], {}).get(name)
        if spec is None:
            missing.append(f"{libs[alias]}.{name}")
            continue
        restype, argtypes = spec
        assert restype is not None, name
        assert isinstance(argtypes, (list, tuple)), name
    assert missing == []
    restype, argtypes = sigs["kernel32"]["GetCurrentProcess"]
    assert restype is wintypes.HANDLE
    assert tuple(argtypes) == ()
    restype, argtypes = sigs["advapi32"]["OpenProcessToken"]
    assert restype is wintypes.BOOL
    assert argtypes[0] is wintypes.HANDLE

    class Fake:
        def __getattr__(self, name):
            if name.startswith("_"):
                raise AttributeError(name)
            fn = type("Fn", (), {"restype": None, "argtypes": None})()
            setattr(self, name, fn)
            return fn

    kernel = Fake()
    adv = Fake()
    contain.bind_signatures(kernel, "kernel32")
    contain.bind_signatures(adv, "advapi32")
    contain.bind_signatures(Fake(), "userenv")
    assert kernel.GetCurrentProcess.restype is wintypes.HANDLE
    assert kernel.GetCurrentProcess.argtypes == []
    assert adv.OpenProcessToken.restype is wintypes.BOOL
    assert adv.OpenProcessToken.argtypes[0] is wintypes.HANDLE
    for library, dll in (("kernel32", kernel), ("advapi32", adv)):
        for name, (restype, argtypes) in sigs[library].items():
            fn = getattr(dll, name)
            assert fn.restype is restype
            assert list(fn.argtypes) == list(argtypes)


def test_derive_without_a_profile_still_creates_one(monkeypatch):
    """DeriveAppContainerSidFromAppContainerName succeeds when no profile exists."""
    from easyagent import contain

    log: list[str] = []

    def dll(library, hr=0):
        made = _WinDll(log, hr)
        contain.bind_signatures(made, library)
        return made

    monkeypatch.setattr(contain, "_dll", lambda library: dll(library))
    contain._container_sid("EasyAgent.Probe", create=True)
    assert log.index("CreateAppContainerProfile") < log.index("DeriveAppContainerSidFromAppContainerName")
    assert log.count("CreateAppContainerProfile") == 1
    assert "DeleteAppContainerProfile" not in log

    log.clear()
    monkeypatch.setattr(contain, "_dll", lambda library: dll(library, hr=-2147024713))
    contain._container_sid("EasyAgent.Probe", create=True)
    assert log.count("CreateAppContainerProfile") == 2
    assert "DeleteAppContainerProfile" in log
    assert "DeriveAppContainerSidFromAppContainerName" in log

    log.clear()
    monkeypatch.setattr(contain, "_dll", lambda library: dll(library, hr=0x80070005))
    with pytest.raises(OSError, match=r"CreateAppContainerProfile failed \(HRESULT 0x80070005\)"):
        contain._container_sid("EasyAgent.Probe", create=True)
    assert "DeriveAppContainerSidFromAppContainerName" not in log


def test_a_derived_sid_is_not_a_profile(monkeypatch, tmp_path):
    from easyagent import contain

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    log: list[str] = []
    monkeypatch.setattr(contain, "_dll", lambda library: _WinDll(log))
    monkeypatch.setattr(contain, "_sid_text", lambda sid: "S-1-15-2-PROBE")
    monkeypatch.setattr(contain, "_mapping_exists", lambda text: False)
    assert contain._profile_exists("EasyAgent.Probe") is False
    assert "DeriveAppContainerSidFromAppContainerName" in log
    assert "CreateAppContainerProfile" not in log

    package = tmp_path / "Packages" / "EasyAgent.Probe"
    package.mkdir(parents=True)
    assert contain._profile_exists("EasyAgent.Probe") is True
    monkeypatch.setattr(contain, "_mapping_exists", lambda text: True)
    package.rmdir()
    assert contain._profile_exists("EasyAgent.Probe") is True


def test_profile_mapping_uses_the_appcontainer_registry_key(monkeypatch):
    import sys
    import types

    from easyagent.contain import _MAPPINGS, _mapping_exists

    opened: list[str] = []

    def open_key(_root, path):
        opened.append(path)
        if path.endswith("missing"):
            raise OSError(2, "not found")
        return object()

    monkeypatch.setitem(
        sys.modules,
        "winreg",
        types.SimpleNamespace(HKEY_CURRENT_USER=1, OpenKey=open_key, CloseKey=lambda _key: None),
    )
    assert _mapping_exists("S-1-15-2-ABC") is True
    assert opened[0] == _MAPPINGS + "\\S-1-15-2-ABC"
    assert "AppContainer\\Mappings" in opened[0]
    assert _mapping_exists("missing") is False


def test_restricted_token_keeps_the_process_handle_wide(monkeypatch):
    from ctypes import wintypes

    from easyagent import contain

    seen = {}

    class Fn(_WinFn):
        def __call__(self, *args):
            if self.name == "GetCurrentProcess":
                seen["restype"] = self.restype
                seen["argtypes"] = list(self.argtypes or [])
            if self.name == "OpenProcessToken":
                seen["handle"] = args[0]
                seen["open_arg0"] = self.argtypes[0]
            return super().__call__(*args)

    class Dll(_WinDll):
        def __getattr__(self, name):
            if name.startswith("_"):
                raise AttributeError(name)
            fn = Fn(name, self.hr)
            setattr(self, name, fn)
            return fn

    def dll(library):
        made = Dll([])
        contain.bind_signatures(made, library)
        return made

    monkeypatch.setattr(contain, "_dll", dll)
    with pytest.raises(OSError, match=r"OpenProcessToken failed \(Win32 \d+\)"):
        contain._create_restricted(Dll([]), ["whoami.exe"], {}, ".", 1)
    assert seen["restype"] is wintypes.HANDLE
    assert seen["argtypes"] == []
    assert seen["handle"] == -1
    assert seen["open_arg0"] is wintypes.HANDLE


def test_the_profile_is_created_with_network_capabilities(monkeypatch):
    import ctypes

    from easyagent import contain

    asked = []
    monkeypatch.setattr(contain, "_sid_from_text", lambda text: asked.append(text) or ctypes.c_void_p(len(asked)))
    seen = {}

    class Fn:
        def __init__(self, name):
            self.name = name
            self.restype = None
            self.argtypes = None

        def __call__(self, *args):
            if self.name == "CreateAppContainerProfile":
                seen["count"] = args[4]
                return 0
            if self.name == "DeriveAppContainerSidFromAppContainerName":
                args[1]._obj.value = 1
                return 0
            return 0

    class Dll:
        def __getattr__(self, name):
            if name.startswith("_"):
                raise AttributeError(name)
            fn = Fn(name)
            setattr(self, name, fn)
            return fn

    monkeypatch.setattr(contain, "_dll", lambda _library: Dll())
    contain._container_sid("EasyAgent.Probe", create=True)
    assert asked == ["S-1-15-3-1", "S-1-15-3-3"]
    assert seen["count"] == 2


def test_launch_carries_both_network_capabilities(monkeypatch):
    import ctypes

    from easyagent import contain

    asked = []

    def sid_from(text):
        asked.append(text)
        return ctypes.c_void_p(len(asked))

    monkeypatch.setattr(contain, "_sid_from_text", sid_from)
    seen = {}

    def fake(_kernel, _argv, _env, _cwd, attribute, value_ptr, _size):
        security = value_ptr._obj
        count = int(security.CapabilityCount)
        seen["attribute"] = attribute
        seen["count"] = count
        seen["sids"] = [int(security.Capabilities[index].Sid) for index in range(count)]

        class Proc:
            def communicate(self, timeout=None):
                return "", ""

        return Proc()

    monkeypatch.setattr(contain, "_create_with_attribute", fake)
    contain._create_appcontainer(None, ["whoami.exe"], {}, ".", 1, True)
    assert seen["attribute"] == SECURITY_CAPABILITIES
    assert asked == ["S-1-15-3-1", "S-1-15-3-3"]
    assert seen["sids"] == [1, 2]


def test_the_restricting_set_is_the_chromium_set(monkeypatch):
    import ctypes

    from easyagent import contain

    assert contain.NETWORK_CAPABILITY_SIDS == ("S-1-15-3-1", "S-1-15-3-3")
    assert contain.RESTRICTING_SIDS == ("S-1-1-0", "S-1-5-32-545", "S-1-5-12")
    assert contain.LOW_INTEGRITY_SID == "S-1-16-4096"
    asked = []
    monkeypatch.setattr(contain, "_sid_from_text", lambda text: asked.append(text) or text)
    sids = contain._restricting_sid_values("user", "deny", "logon")
    assert sids == ["user", "deny", "S-1-1-0", "S-1-5-32-545", "S-1-5-12", "logon"]
    assert asked == ["S-1-1-0", "S-1-5-32-545", "S-1-5-12"]
    assert contain._restricting_sid_values("user", "deny", None)[-1] == "S-1-5-12"

    seen = {}
    monkeypatch.setattr(contain, "_sid_from_text", lambda text: ctypes.c_void_p(66))
    monkeypatch.setattr(contain, "_token_user_sid", lambda _advapi, _token: ctypes.c_void_p(11))
    monkeypatch.setattr(contain, "_logon_sid", lambda _advapi, _token: ctypes.c_void_p(12))
    order = []

    def low(_advapi, token):
        order.append("low")
        seen["integrity"] = token

    def launch(_advapi, token, _command, _env, _cwd):
        order.append("launch")
        seen["token"] = token

        class Proc:
            def communicate(self, timeout=None):
                return "ok", ""

        return Proc()

    monkeypatch.setattr(contain, "_low_integrity", low)
    monkeypatch.setattr(contain, "_CreateProcessAsUser", launch)

    class Fn:
        def __init__(self, name):
            self.name = name
            self.restype = None
            self.argtypes = None

        def __call__(self, *args):
            if self.name == "CreateRestrictedToken":
                seen["flags"] = args[1]
                seen["count"] = args[6]
            if self.name == "OpenProcessToken":
                return 1
            if self.name == "GetCurrentProcess":
                return -1
            return 1

    class Dll:
        def __getattr__(self, name):
            if name.startswith("_"):
                raise AttributeError(name)
            fn = Fn(name)
            setattr(self, name, fn)
            return fn

    def dll(library):
        made = Dll()
        contain.bind_signatures(made, library)
        return made

    monkeypatch.setattr(contain, "_dll", dll)
    kernel = Dll()
    contain.bind_signatures(kernel, "kernel32")
    contain._create_restricted(kernel, ["whoami.exe"], {}, ".", ctypes.c_void_p(99))
    assert seen["count"] == 6
    assert seen["flags"] == 0x1
    assert order == ["low", "launch"]
    assert seen["integrity"] is seen["token"]


def test_low_integrity_uses_the_low_label(monkeypatch):
    import ctypes

    from easyagent import contain

    seen = {}

    def sid_from(text):
        seen["sid"] = text
        return ctypes.c_void_p(66)

    monkeypatch.setattr(contain, "_sid_from_text", sid_from)

    class Api:
        def SetTokenInformation(self, _token, level, _label, size):
            seen["level"] = level
            seen["size"] = size
            return 1

    contain._low_integrity(Api(), 7)
    assert seen["sid"] == "S-1-16-4096"
    assert seen["level"] == 25
    assert seen["size"] > 0


def test_logon_sid_is_the_group_marked_logon():
    import ctypes
    from ctypes import wintypes

    from easyagent.contain import _logon_sid

    class _SidAttr(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class _Groups(ctypes.Structure):
        _fields_ = [("GroupCount", wintypes.DWORD), ("Groups", _SidAttr * 2)]

    groups = _Groups()
    groups.GroupCount = 2
    groups.Groups[0].Sid = ctypes.c_void_p(5)
    groups.Groups[0].Attributes = 0x4
    groups.Groups[1].Sid = ctypes.c_void_p(9)
    groups.Groups[1].Attributes = 0xC0000000
    raw = ctypes.string_at(ctypes.addressof(groups), ctypes.sizeof(groups))

    class Api:
        def GetTokenInformation(self, _token, _klass, buf, size, needed):
            if not size:
                needed._obj.value = len(raw)
                return 0
            ctypes.memmove(buf, raw, len(raw))
            return 1

    assert int(_logon_sid(Api(), 1) or 0) == 9


def test_a_dead_child_is_not_a_denied_read():
    from easyagent.selftest import _record_reads

    class Dead:
        returncode = 0xC0000022

        def communicate(self, timeout=None):
            return "", ""

    rows = []
    _record_reads(rows, lambda _argv: Dead(), Path("C:/data"), "CANARY-1", "SECRET-2")
    found = {name: (status, detail) for name, status, detail in rows}
    assert found["control"][0] == "FAIL"
    assert "0xC0000022" in found["control"][1]
    assert found["data-read"][0] == "INCONCLUSIVE"
    assert found["secrets"][0] == "INCONCLUSIVE"


def test_a_live_denial_passes_and_a_leak_fails():
    from easyagent.selftest import _record_reads

    def launch(argv):
        text = " ".join(argv)

        class Proc:
            def communicate(self, timeout=None):
                if "EASYAGENT-RAN" in text:
                    self.returncode = 0
                    return "EASYAGENT-RAN\n", ""
                if "endpoints.json" in text:
                    self.returncode = 1
                    return "", "Access is denied"
                self.returncode = 0
                return "SECRET-2", ""

        return Proc()

    rows = []
    assert _record_reads(rows, launch, Path("C:/data"), "CANARY-1", "SECRET-2") is True
    found = {name: status for name, status, _detail in rows}
    assert found["control"] == "PASS"
    assert found["data-read"] == "PASS"
    assert found["secrets"] == "FAIL"


def test_a_dead_read_after_a_live_control_is_inconclusive():
    from easyagent.selftest import _record_reads

    def launch(argv):
        text = " ".join(argv)

        class Proc:
            def communicate(self, timeout=None):
                if "EASYAGENT-RAN" in text:
                    self.returncode = 0
                    return "EASYAGENT-RAN\n", ""
                self.returncode = 0xC0000022
                return "", ""

        return Proc()

    rows = []
    _record_reads(rows, launch, Path("C:/data"), "CANARY-1", "SECRET-2")
    found = {name: status for name, status, _detail in rows}
    assert found["control"] == "PASS"
    assert found["data-read"] == "INCONCLUSIVE"
    assert found["secrets"] == "INCONCLUSIVE"


def test_the_probe_runs_the_restricted_token_on_its_own(monkeypatch):
    from easyagent import contain, selftest

    calls = []
    monkeypatch.setattr(contain, "_container_sid", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(contain, "_sid_text", lambda _sid: "S-1-15-2-X")
    monkeypatch.setattr(contain, "_grant_rights", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(contain, "_grant_sid", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(contain, "_delete_profile", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(contain, "_profile_exists", lambda _name: False)
    monkeypatch.setattr(contain, "_remove_ace", lambda _ace: None)
    monkeypatch.setattr(contain, "_dll", lambda _library: object())
    monkeypatch.setattr(selftest, "_record_launch", lambda *_args, **_kwargs: calls.append("app"))
    monkeypatch.setattr(selftest, "_record_restricted", lambda *_args, **_kwargs: calls.append("restricted"))
    monkeypatch.setattr("easyagent.sandbox.web_enabled", lambda: False)
    selftest._windows_probe([])
    assert calls == ["app", "restricted"]

    calls.clear()

    def boom(*_args, **_kwargs):
        calls.append("app")
        raise OSError("CreateProcessW failed (Win32 2)")

    monkeypatch.setattr(selftest, "_record_launch", boom)
    selftest._windows_probe([])
    assert calls == ["app", "restricted"]


def test_an_invented_sid_deny_is_applied_and_read_back(tmp_path, monkeypatch):
    import ctypes

    from easyagent import contain

    assert ctypes.sizeof(contain._TRUSTEE_W) == (32 if ctypes.sizeof(ctypes.c_void_p) == 8 else 20)
    sid = contain._sid_bytes("S-1-5-59923")
    assert sid[0] == 1
    assert sid[1] == 1
    assert sid[2:8] == bytes([0, 0, 0, 0, 0, 5])
    assert int.from_bytes(sid[8:12], "little") == 59923
    deny = _acl_with(sid, contain.ACCESS_DENIED_ACE_TYPE, contain.FILE_ALL_ACCESS, 0x3)
    assert contain._ace_in_blob(deny, sid, contain.ACCESS_DENIED_ACE_TYPE, contain.FILE_ALL_ACCESS, 0x3)
    weak = _acl_with(sid, contain.ACCESS_DENIED_ACE_TYPE, contain.FILE_TRAVERSE, 0x3)
    assert not contain._ace_in_blob(weak, sid, contain.ACCESS_DENIED_ACE_TYPE, contain.FILE_ALL_ACCESS, 0x3)
    assert not contain._ace_in_blob(deny, sid, contain.ACCESS_ALLOWED_ACE_TYPE, contain.FILE_ALL_ACCESS, 0x3)

    class Api:
        def __init__(self):
            self.blob = bytes([2, 0, 8, 0, 0, 0, 0, 0])
            self.seen = {}
            self.reads = 0
            self.ace_reads = 0
            self.writes = 0
            self.fail = 0
            self._keep = []

        def GetNamedSecurityInfoW(self, _name, _kind, _info, _owner, _group, dacl, _sacl, sd):
            self.reads += 1
            buf = ctypes.create_string_buffer(self.blob)
            self._keep.append(buf)
            ptr = ctypes.cast(buf, ctypes.c_void_p).value
            dacl._obj.value = ptr
            sd._obj.value = ptr
            return 0

        def GetSecurityDescriptorControl(self, _sd, control, revision):
            control._obj.value = 0
            revision._obj.value = 1
            return 1

        def SetEntriesInAclW(self, _count, entries, _old, new_acl):
            if self.fail:
                return self.fail
            entry = entries._obj
            self.seen = {
                "mode": int(entry.grfAccessMode),
                "mask": int(entry.grfAccessPermissions),
                "inherit": int(entry.grfInheritance),
                "form": int(entry.Trustee.TrusteeForm),
            }
            sid_ptr = int(entry.Trustee.ptstrName or 0)
            raw = ctypes.string_at(sid_ptr, len(sid))
            built = _acl_with(raw, contain.ACCESS_DENIED_ACE_TYPE, int(entry.grfAccessPermissions), int(entry.grfInheritance))
            buf = ctypes.create_string_buffer(built)
            self._keep.append(buf)
            self.pending = built
            new_acl._obj.value = ctypes.cast(buf, ctypes.c_void_p).value
            return 0

        def SetNamedSecurityInfoW(self, _name, _kind, _info, _owner, _group, dacl, _sacl):
            self.writes += 1
            address = int(dacl.value)
            size = int.from_bytes(ctypes.string_at(address, 4)[2:4], "little")
            self.blob = ctypes.string_at(address, size)
            return 0

        def GetAce(self, dacl, index, out):
            self.ace_reads += 1
            address = int(dacl.value)
            size = int.from_bytes(ctypes.string_at(address, 4)[2:4], "little")
            blob = ctypes.string_at(address, size)
            offset = 8
            for current in range(int.from_bytes(blob[4:6], "little")):
                ace_size = int.from_bytes(blob[offset + 2 : offset + 4], "little")
                if current == int(index):
                    out._obj.value = address + offset
                    return 1
                offset += ace_size
            return 0

        def EqualSid(self, left, right):
            count = ctypes.string_at(int(left.value), 2)[1]
            size = 8 + 4 * count
            same = ctypes.string_at(int(left.value), size) == ctypes.string_at(int(right.value), size)
            return 1 if same else 0

        def LocalFree(self, _ptr):
            return None

    api = Api()
    held = ctypes.create_string_buffer(sid)
    pointer = ctypes.cast(held, ctypes.c_void_p)
    folder = tmp_path / "data"
    folder.mkdir()

    monkeypatch.setattr(contain, "_dll", lambda _library: api)
    contain._deny_sid_on(pointer, folder)
    assert api.seen["mode"] == contain.DENY_ACCESS
    assert api.seen["mask"] == contain.FILE_ALL_ACCESS
    assert api.seen["inherit"] == contain.OBJECT_INHERIT_ACE | contain.CONTAINER_INHERIT_ACE
    assert api.seen["form"] == contain.TRUSTEE_IS_SID
    assert api.reads >= 2
    assert api.ace_reads >= 1
    assert api.writes == 1
    assert contain._ace_in_blob(api.blob, sid, contain.ACCESS_DENIED_ACE_TYPE, contain.FILE_ALL_ACCESS, 0x3)
    api.fail = 1332
    api.writes = 0
    with pytest.raises(contain.AclError, match=r"Win32 1332") as caught:
        contain._deny_sid_on(pointer, folder)
    assert "The restricted token was not started." in str(caught.value)
    assert api.writes == 0
    assert "icacls" not in Path(contain.__file__).read_text(encoding="utf-8")


def _acl_with(sid: bytes, ace_type: int, mask: int, flags: int) -> bytes:
    ace_size = 8 + len(sid)
    ace = bytearray(ace_size)
    ace[0] = ace_type
    ace[1] = flags
    ace[2:4] = ace_size.to_bytes(2, "little")
    ace[4:8] = (mask & 0xFFFFFFFF).to_bytes(4, "little")
    ace[8:] = sid
    acl_size = 8 + ace_size
    acl = bytearray(acl_size)
    acl[0] = 2
    acl[2:4] = acl_size.to_bytes(2, "little")
    acl[4:6] = (1).to_bytes(2, "little")
    acl[8:] = ace
    return bytes(acl)


def test_a_recorded_deny_is_rewritten_when_the_ace_is_missing(monkeypatch, tmp_path):
    import ctypes

    from easyagent import contain

    ledger = {
        "consented": True,
        "aces": [{"action": "deny", "path": str(tmp_path), "sid": "S-1-5-59923", "rights": "(OI)(CI)F"}],
    }
    monkeypatch.setattr(contain, "load_ledger", lambda: ledger)
    monkeypatch.setattr(contain, "_deny_sid", lambda: ctypes.c_void_p(1))
    monkeypatch.setattr(contain, "_sid_text", lambda _sid: "S-1-5-59923")
    monkeypatch.setattr(contain, "_protect_folder", lambda _folder: None)
    monkeypatch.setattr(contain, "_sid_from_text", lambda text: ctypes.c_void_p(2))
    monkeypatch.setattr(contain, "_dll", lambda _library: object())
    monkeypatch.setattr(contain, "_audit_contain", lambda *_args, **_kwargs: None)
    saved = []
    monkeypatch.setattr(contain, "save_ledger", lambda data: saved.append(data))
    calls = []

    def missing(*_args, **_kwargs):
        raise contain.AclError(0, "The ACE was not on the DACL after it was written. The restricted token was not started.")

    monkeypatch.setattr(contain, "_verify_ace", missing)
    monkeypatch.setattr(contain, "_deny_sid_on", lambda *_args, **_kwargs: calls.append("deny"))
    contain._ensure_data_deny(tmp_path)
    assert calls == ["deny"]
    assert saved == []

    calls.clear()
    monkeypatch.setattr(contain, "_verify_ace", lambda *_args, **_kwargs: None)
    contain._ensure_data_deny(tmp_path)
    assert calls == []


def test_a_failed_ace_blocks_the_restricted_launch(monkeypatch, tmp_path):
    import ctypes

    from easyagent import contain

    monkeypatch.setattr(
        "easyagent.selftest.cached_probe",
        lambda: {"passed": True, "mechanism": "restricted-token"},
    )
    monkeypatch.setattr(contain, "load_ledger", lambda: {"consented": True, "aces": []})
    monkeypatch.setattr(contain, "_deny_sid", lambda: ctypes.c_void_p(1))
    monkeypatch.setattr(contain, "_sid_text", lambda _sid: "S-1-5-59923")
    monkeypatch.setattr(contain, "_protect_folder", lambda _folder: None)

    def boom(*_args, **_kwargs):
        raise contain.AclError(1332, "SetEntriesInAclW failed (Win32 1332). The restricted token was not started.")

    monkeypatch.setattr(contain, "_deny_sid_on", boom)
    spawned = []
    monkeypatch.setattr(contain, "_spawn_windows", lambda *_args, **_kwargs: spawned.append(True))
    with pytest.raises(contain.AclError, match="not started"):
        contain._launch_windows(["whoami.exe"], {}, str(tmp_path), tmp_path, [], False)
    assert spawned == []


def test_a_failed_deny_does_not_launch_the_restricted_child(monkeypatch, tmp_path):
    import ctypes

    from easyagent import contain, selftest

    monkeypatch.setattr(contain, "_deny_sid", lambda: ctypes.c_void_p(1))
    monkeypatch.setattr(contain, "_sid_text", lambda _sid: "S-1-5-59923")
    monkeypatch.setattr(contain, "_protect_folder", lambda _folder: None)
    monkeypatch.setattr(contain, "_grant_sid", lambda *_args, **_kwargs: None)
    launched = []
    monkeypatch.setattr(contain, "_create_restricted", lambda *_args, **_kwargs: launched.append(True))

    def boom(*_args, **_kwargs):
        raise contain.AclError(1332, "SetNamedSecurityInfoW failed (Win32 1332). The restricted token was not started.")

    monkeypatch.setattr(contain, "_deny_sid_on", boom)
    data = tmp_path / "data"
    work = data / "workspace"
    data.mkdir()
    work.mkdir()
    with pytest.raises(contain.AclError, match="not started"):
        selftest._record_restricted([], data, work, "CANARY", "SECRET", [])
    assert launched == []


def test_a_failed_ace_does_not_start_an_ordinary_process(tmp_path, monkeypatch):
    from easyagent import turn as turn_mod
    from easyagent.contain import AclError
    from easyagent.mcpclient import launch_stdio
    from easyagent.tools import ToolError, _run_shell

    monkeypatch.setenv("EASYAGENT_CONTAIN_LEDGER", str(tmp_path / "contain.json"))
    store, bot = _bot(tmp_path)
    from easyagent.contain import save_ledger

    save_ledger({"consented": True, "profile": "", "sid": "", "aces": [], "platform": "test"})
    monkeypatch.setattr(
        "easyagent.selftest.cached_probe",
        lambda: {"passed": True, "mechanism": "restricted-token", "status": "active", "reason": "", "rows": []},
    )

    def boom(*_args, **_kwargs):
        raise AclError(1332, "SetEntriesInAclW failed (Win32 1332). The restricted token was not started.")

    monkeypatch.setattr("easyagent.sandbox.popen_contained", boom)
    monkeypatch.setattr("easyagent.mcpclient._popen_contained", boom)
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("ordinary process")))
    slot = turn_mod.slot_for(store, "chat-ace")
    slot.bot_id = bot["id"]
    slot.store = store
    slot.cwd = str(tmp_path)
    turn_mod._slot.set(slot)
    try:
        with pytest.raises(ToolError, match="not started"):
            _run_shell(store, "echo plain-ok")
        with pytest.raises(AclError, match="not started"):
            launch_stdio(["server"], {}, str(tmp_path), store, bot["id"])
    finally:
        turn_mod._slot.set(None)
        save_ledger({"consented": False, "profile": "", "aces": []})


def test_own_addresses_are_info_and_a_firewall_block_is_a_warning(monkeypatch):
    from easyagent import selftest

    monkeypatch.setattr(selftest, "_host_lan_ip", lambda: "203.0.113.10")
    monkeypatch.setattr(selftest, "third_party_firewalls", lambda: ["Bitdefender Firewall"])

    def launch(argv):
        text = " ".join(argv)

        class Proc:
            def communicate(self, timeout=None):
                self.returncode = 0
                if "1.1.1.1" in text:
                    return "WEB DENIED forbidden by its access permissions", ""
                if "127.0.0.1" in text:
                    return "OWN ok", ""
                return "OWN DENIED timeout", ""

        return Proc()

    rows = []
    selftest._record_web(rows, launch)
    found = {name: (status, detail) for name, status, detail in rows}
    assert found["loopback"][0] == "INFO"
    assert found["lan"][0] == "INFO"
    assert "203.0.113.10" in found["lan"][1]
    assert found["web"][0] == "WARN"
    assert selftest.FIREWALL_WARN in found["web"][1]
    script = " ".join(selftest._tcp_script("1.1.1.1", 443, "WEB"))
    assert "1.1.1.1" in script
    assert ".Send" not in script

    monkeypatch.setattr(selftest, "third_party_firewalls", lambda: [])
    rows = []
    selftest._record_web(rows, launch)
    found = {name: (status, detail) for name, status, detail in rows}
    assert found["web"][0] == "WARN"
    assert "WEB DENIED" in found["web"][1]

    def dead(argv):
        text = " ".join(argv)

        class Proc:
            returncode = 0xC0000022

            def communicate(self, timeout=None):
                if "1.1.1.1" in text:
                    return "", ""
                self.returncode = 0
                return "OWN DENIED timeout", ""

        return Proc()

    rows = []
    selftest._record_web(rows, dead)
    found = {name: status for name, status, _detail in rows}
    assert found["web"] == "WARN"


def test_network_does_not_decide_containment():
    from easyagent.selftest import _windows_passed

    names = [
        "whoami",
        "control",
        "data-read",
        "secrets",
        "workspace-write",
        "restricted-whoami",
        "restricted-control",
        "restricted-data-read",
        "restricted-secrets",
        "restricted-workspace-write",
    ]
    rows = [(name, "PASS", "") for name in names]
    rows.append(("web", "WARN", "a third-party firewall is blocking sandboxed bots from the network; EasyAgent's own web search still works"))
    rows.append(("loopback", "INFO", "loopback"))
    rows.append(("cleanup", "PASS", "profile removed"))
    assert _windows_passed(rows, True) is True
    warned = [(name, "FAIL" if name == "web" else status, detail) for name, status, detail in rows]
    assert _windows_passed(warned, True) is True
    leaked = [(name, "FAIL" if name == "restricted-data-read" else status, detail) for name, status, detail in rows]
    assert _windows_passed(leaked, True) is False


def test_third_party_firewall_names_skip_windows(monkeypatch):
    from easyagent import selftest

    monkeypatch.setattr(selftest, "_on_windows", lambda: False)
    assert selftest.third_party_firewalls() == []
    monkeypatch.setattr(selftest, "_on_windows", lambda: True)

    class Proc:
        stdout = "Windows Defender Firewall\nBitdefender Firewall\nZoneAlarm Free\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: Proc())
    assert selftest.third_party_firewalls() == ["Bitdefender Firewall", "ZoneAlarm Free"]

    def timed_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("powershell", 15)

    monkeypatch.setattr(subprocess, "run", timed_out)
    assert selftest.third_party_firewalls() == []


def test_search_and_the_model_stay_in_this_process(monkeypatch):
    from easyagent import contain, llm, search

    for module in (search, llm):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "popen_contained" not in source
        assert "httpx" in source
    assert "this process" in (search.web_search.__doc__ or "")
    assert "this process" in (llm.complete.__doc__ or "")

    def refuse(*_args, **_kwargs):
        raise AssertionError("contained")

    monkeypatch.setattr(contain, "popen_contained", refuse)

    async def fake_get(_url, params=None, headers=None):
        del params, headers
        return '{"AbstractText":"","RelatedTopics":[]}'

    monkeypatch.setattr(search, "_get_text", fake_get)
    heard = asyncio.run(search.web_search("hello"))
    assert heard == "The search found nothing."


def test_rights_tokens_include_traverse_in_parentheses():
    """(X) ends in a parenthesis. The mask is the X, not the last character."""
    from easyagent import contain

    inherit = contain.OBJECT_INHERIT_ACE | contain.CONTAINER_INHERIT_ACE
    expected = {
        "(X)": (contain.FILE_TRAVERSE, 0),
        "X": (contain.FILE_TRAVERSE, 0),
        "(OI)(CI)M": (contain.FILE_MODIFY, inherit),
        "(OI)(CI)(M)": (contain.FILE_MODIFY, inherit),
        "(OI)(CI)F": (contain.FILE_ALL_ACCESS, inherit),
        "F": (contain.FILE_ALL_ACCESS, 0),
        "M": (contain.FILE_MODIFY, 0),
        "RX": (contain.FILE_READ_EXECUTE, 0),
        "(RX)": (contain.FILE_READ_EXECUTE, 0),
        "R": (contain.FILE_GENERIC_READ, 0),
        "W": (contain.FILE_GENERIC_WRITE, 0),
        "(R,W)": (contain.FILE_GENERIC_READ | contain.FILE_GENERIC_WRITE, 0),
        "(OI)(CI)RX": (contain.FILE_READ_EXECUTE, inherit),
        "RWX": (contain.FILE_GENERIC_READ | contain.FILE_GENERIC_WRITE | contain.FILE_TRAVERSE, 0),
    }
    assert contain.FILE_MODIFY == (
        contain.FILE_GENERIC_READ | contain.FILE_GENERIC_WRITE | contain.FILE_GENERIC_EXECUTE | 0x00010000
    )
    for rights, (mask, flags) in expected.items():
        assert contain._mask_for(rights) == mask, rights
        assert contain._inherit_for(rights) == flags, rights
    source = Path(contain.__file__).read_text(encoding="utf-8")
    probe = Path(contain.__file__).parent.joinpath("selftest.py").read_text(encoding="utf-8")
    for rights in ("(X)", "(OI)(CI)M", "(OI)(CI)F", "F"):
        assert rights in source or rights in probe


def test_a_traverse_grant_uses_the_traverse_mask(monkeypatch, tmp_path):
    from easyagent import contain

    seen = {}

    def apply(_sid, path, mode, mask, inheritance):
        seen["path"] = path
        seen["mode"] = mode
        seen["mask"] = mask
        seen["inheritance"] = inheritance

    monkeypatch.setattr(contain, "_apply_ace", apply)
    contain._grant_rights(object(), tmp_path, "(X)")
    assert seen["mask"] == contain.FILE_TRAVERSE
    assert seen["inheritance"] == 0
    assert seen["mode"] == contain.GRANT_ACCESS


def test_a_modify_grant_sets_the_low_label(monkeypatch, tmp_path):
    from easyagent import contain

    calls = []
    monkeypatch.setattr(contain, "_apply_ace", lambda *_args, **_kwargs: calls.append("ace"))
    monkeypatch.setattr(contain, "_set_low_label", lambda folder: calls.append(folder))
    contain._grant_sid(object(), [tmp_path])
    assert calls == ["ace", tmp_path]


def test_probe_scratch_is_under_local_app_data(tmp_path, monkeypatch):
    import shutil

    from easyagent.selftest import _probe_scratch

    local = tmp_path / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    scratch = _probe_scratch()
    try:
        assert scratch.is_dir()
        assert scratch.parent == local / "EasyAgent"
        assert scratch.name.startswith("probe-")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_probe_scratch_falls_back_to_the_user_profile(tmp_path, monkeypatch):
    import shutil

    from easyagent.selftest import _probe_scratch

    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    scratch = _probe_scratch()
    try:
        assert scratch.parent == tmp_path / "profile" / "AppData" / "Local" / "EasyAgent"
    finally:
        shutil.rmtree(scratch.parent.parent.parent, ignore_errors=True)


def test_a_denied_label_claims_the_workspace_and_retries(tmp_path, monkeypatch):
    from easyagent import contain

    folder = tmp_path / "work"
    folder.mkdir()
    calls = {"apply": 0, "claim": 0}

    def apply(path):
        calls["apply"] += 1
        if calls["apply"] == 1:
            raise contain.AclError(5, "SetNamedSecurityInfoW failed (Win32 5). The restricted token was not started.")
        assert path == folder

    def claim(path):
        calls["claim"] += 1
        assert path == folder

    monkeypatch.setattr(contain, "_apply_low_label", apply)
    monkeypatch.setattr(contain, "_claim_workspace", claim)
    contain._set_low_label(folder)
    assert calls == {"apply": 2, "claim": 1}


def test_a_second_label_denial_refuses_the_launch(tmp_path, monkeypatch):
    from easyagent import contain

    folder = tmp_path / "work"
    folder.mkdir()

    def apply(_path):
        raise contain.AclError(5, "SetNamedSecurityInfoW failed (Win32 5). The restricted token was not started.")

    monkeypatch.setattr(contain, "_apply_low_label", apply)
    monkeypatch.setattr(contain, "_claim_workspace", lambda _path: None)
    with pytest.raises(contain.AclError, match="cannot own that workspace"):
        contain._set_low_label(folder)


def test_a_label_error_other_than_access_denied_does_not_take_ownership(tmp_path, monkeypatch):
    from easyagent import contain

    folder = tmp_path / "work"
    folder.mkdir()
    claimed = []
    monkeypatch.setattr(
        contain,
        "_apply_low_label",
        lambda _path: (_ for _ in ()).throw(
            contain.AclError(1332, "SetNamedSecurityInfoW failed (Win32 1332). The restricted token was not started.")
        ),
    )
    monkeypatch.setattr(contain, "_claim_workspace", lambda path: claimed.append(path))
    with pytest.raises(contain.AclError, match="Win32 1332"):
        contain._set_low_label(folder)
    assert claimed == []


def test_rebuilding_the_dacl_drops_a_deny_and_the_label_comes_off(tmp_path, monkeypatch):
    import ctypes

    from easyagent import contain

    deny_sid = contain._sid_bytes("S-1-5-59923")
    other_sid = contain._sid_bytes("S-1-1-0")
    low_sid = contain._sid_bytes(contain.LOW_INTEGRITY_SID)
    deny = _acl_with(deny_sid, contain.ACCESS_DENIED_ACE_TYPE, contain.FILE_ALL_ACCESS, 0x3)[8:]
    other = _acl_with(other_sid, contain.ACCESS_ALLOWED_ACE_TYPE, contain.FILE_GENERIC_READ, 0)[8:]
    original = contain._pack_acl([other, deny])
    rebuilt = contain._acl_without_sid(original, deny_sid)
    assert contain._ace_in_blob(rebuilt, other_sid, contain.ACCESS_ALLOWED_ACE_TYPE, contain.FILE_GENERIC_READ, 0)
    assert not contain._ace_in_blob(rebuilt, deny_sid, contain.ACCESS_DENIED_ACE_TYPE, contain.FILE_ALL_ACCESS, 0x3)
    label = contain._label_acl(low_sid)
    assert contain._ace_in_blob(
        label,
        low_sid,
        contain.SYSTEM_MANDATORY_LABEL_ACE_TYPE,
        contain.SYSTEM_MANDATORY_LABEL_NO_WRITE_UP,
        0x3,
    )
    assert contain._acl_without_sid(label, low_sid)[4:6] == b"\x00\x00"

    class Api:
        def __init__(self):
            self.dacl = original
            self.label = b""
            self.getace = 0
            self.setentries = 0
            self.label_sets = 0
            self._keep = []

        def GetNamedSecurityInfoW(self, _name, _kind, info, _owner, _group, dacl, sacl, sd):
            info = int(info)
            holder = ctypes.create_string_buffer(b"\0" * 8)
            self._keep.append(holder)
            sd._obj.value = ctypes.cast(holder, ctypes.c_void_p).value
            if info & contain.DACL_SECURITY_INFORMATION:
                buf = ctypes.create_string_buffer(self.dacl)
                self._keep.append(buf)
                dacl._obj.value = ctypes.cast(buf, ctypes.c_void_p).value
            else:
                dacl._obj.value = 0
            if info & contain.LABEL_SECURITY_INFORMATION and self.label:
                buf = ctypes.create_string_buffer(self.label)
                self._keep.append(buf)
                sacl._obj.value = ctypes.cast(buf, ctypes.c_void_p).value
            else:
                sacl._obj.value = 0
            return 0

        def SetNamedSecurityInfoW(self, _name, _kind, info, _owner, _group, dacl, sacl):
            info = int(info)
            if info & contain.DACL_SECURITY_INFORMATION:
                address = int(dacl.value)
                size = int.from_bytes(ctypes.string_at(address, 4)[2:4], "little")
                self.dacl = bytes(ctypes.string_at(address, size))
            if info & contain.LABEL_SECURITY_INFORMATION:
                self.label_sets += 1
                if sacl is None or not int(getattr(sacl, "value", 0) or 0):
                    self.label = b""
                else:
                    address = int(sacl.value)
                    size = int.from_bytes(ctypes.string_at(address, 4)[2:4], "little")
                    self.label = bytes(ctypes.string_at(address, size))
            return 0

        def GetSecurityDescriptorControl(self, _sd, control, revision):
            control._obj.value = 0
            revision._obj.value = 1
            return 1

        def GetAce(self, acl, index, out):
            self.getace += 1
            address = int(acl.value)
            size = int.from_bytes(ctypes.string_at(address, 4)[2:4], "little")
            blob = ctypes.string_at(address, size)
            offset = 8
            for current in range(int.from_bytes(blob[4:6], "little")):
                ace_size = int.from_bytes(blob[offset + 2 : offset + 4], "little")
                if current == int(index):
                    out._obj.value = address + offset
                    return 1
                offset += ace_size
            return 0

        def EqualSid(self, left, right):
            count = ctypes.string_at(int(left.value), 2)[1]
            size = 8 + 4 * count
            same = ctypes.string_at(int(left.value), size) == ctypes.string_at(int(right.value), size)
            return 1 if same else 0

        def SetEntriesInAclW(self, *_args):
            self.setentries += 1
            return 1332

        def LocalFree(self, _ptr):
            return None

    api = Api()
    monkeypatch.setattr(contain, "_dll", lambda _library: api)
    held = {}

    def sid_from(text):
        raw = contain._sid_bytes(text)
        buf = ctypes.create_string_buffer(raw)
        held[text] = buf
        return ctypes.cast(buf, ctypes.c_void_p)

    monkeypatch.setattr(contain, "_sid_from_text", sid_from)
    folder = tmp_path / "work"
    folder.mkdir()
    contain._set_low_label(folder)
    assert api.label_sets == 1
    assert contain._ace_in_blob(
        api.label,
        low_sid,
        contain.SYSTEM_MANDATORY_LABEL_ACE_TYPE,
        contain.SYSTEM_MANDATORY_LABEL_NO_WRITE_UP,
        0x3,
    )
    contain._remove_ace({"action": "deny", "path": str(folder), "sid": "S-1-5-59923", "rights": "(OI)(CI)F"})
    assert api.setentries == 0
    assert api.getace > 0
    assert not contain._ace_in_blob(api.dacl, deny_sid, contain.ACCESS_DENIED_ACE_TYPE, contain.FILE_ALL_ACCESS, 0x3)
    assert contain._ace_in_blob(api.dacl, other_sid, contain.ACCESS_ALLOWED_ACE_TYPE, contain.FILE_GENERIC_READ, 0)
    assert api.label
    contain._remove_ace({"action": "grant", "path": str(folder), "sid": "S-1-1-0", "rights": "(OI)(CI)M"})
    assert api.label == b"" or int.from_bytes(api.label[4:6], "little") == 0
    assert not contain._ace_in_blob(api.dacl, other_sid, contain.ACCESS_ALLOWED_ACE_TYPE, contain.FILE_GENERIC_READ, 0)
    contain._remove_low_label(folder)
