"""AI agents, made easy."""

import os as _os

__version__ = "0.3.23"


def _drop_inherited_tls_keylog() -> None:
    """Ignore an inherited SSLKEYLOGFILE.

    Python's ``ssl.create_default_context()`` (which httpx calls for every client,
    even for an ``http://`` connection) opens SSLKEYLOGFILE for append. Some
    security suites set it for every process (Bitdefender points it at
    ``\\\\?\\Volume{...}\\virtual_file.log`` on its own virtual disk), and an open that
    fails raises PermissionError, which stopped every chat turn. Writing TLS
    session secrets is also something EasyAgent should never do by accident: it
    carries provider API keys. A developer who wants a key log for debugging
    sets EASYAGENT_ALLOW_SSLKEYLOGFILE=1.
    """
    if _os.environ.get("EASYAGENT_ALLOW_SSLKEYLOGFILE", "").strip() == "1":
        return
    _os.environ.pop("SSLKEYLOGFILE", None)


_drop_inherited_tls_keylog()
