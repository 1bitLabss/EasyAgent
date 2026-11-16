"""A self-signed certificate so phones can use the home-screen install.

Browsers only offer the install over an encrypted address, so the LAN
socket gets a short-lived self-signed certificate when Phone access is
turned on. The computer itself keeps using the plain address.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import ssl
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from easyagent.access import lan_ip

_DAYS = 396  # plus the one-day backdate below, the whole span stays under 397 days
_SOON = timedelta(days=30)
_CACHE: dict[str, tuple[int, int, ssl.SSLContext]] = {}
_LOCK = threading.Lock()


def tls_dir(root: Path) -> Path:
    return Path(root) / "tls"


def _chmod(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        return


def _addresses() -> list[str]:
    """Loopback, the address other phones use, and anything else this machine has."""
    ips = ["127.0.0.1"]
    primary = lan_ip()
    if primary and primary not in ips:
        ips.append(primary)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = str(info[4][0])
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def _issue(cert_path: Path, key_path: Path) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "EasyAgent")])
    names = x509.SubjectAlternativeName(
        [x509.IPAddress(ipaddress.ip_address(ip)) for ip in _addresses()]
        + [x509.DNSName(socket.gethostname())]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(names, critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    _chmod(key_path)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    _chmod(cert_path)


def _usable(cert_path: Path, key_path: Path) -> bool:
    """Reuse only a pair that parses, is not near its end, and covers today's address."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        now = datetime.now(timezone.utc)
        if cert.not_valid_after_utc <= now + _SOON:
            return False
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except Exception:
        return False
    primary = lan_ip()
    if primary and primary not in {str(ip) for ip in names.get_values_for_type(x509.IPAddress)}:
        return False
    return True


def ensure(root: Path) -> Path | None:
    """cert.pem and key.pem under the phone data dir, fresh. None if that is not possible."""
    try:
        cert_path = tls_dir(root) / "cert.pem"
        key_path = tls_dir(root) / "key.pem"
        if not (cert_path.is_file() and key_path.is_file()) or not _usable(cert_path, key_path):
            _issue(cert_path, key_path)
        return cert_path
    except Exception:
        return None


def ssl_context(root: Path) -> ssl.SSLContext | None:
    """The server side of the encrypted address, kept until the files change."""
    try:
        cert_path = ensure(root)
        if cert_path is None:
            return None
        key_path = cert_path.with_name("key.pem")
        stamp = (cert_path.stat().st_mtime_ns, key_path.stat().st_mtime_ns)
        name = str(cert_path.parent)
        with _LOCK:
            cached = _CACHE.get(name)
            if cached is None or cached[:2] != stamp:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(cert_path, keyfile=key_path)
                cached = (stamp[0], stamp[1], context)
                _CACHE[name] = cached
        return cached[2]
    except Exception:
        return None
