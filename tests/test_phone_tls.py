"""The LAN socket gets a self-signed certificate so phones can install the app.

The certificate lives under the phone data dir, covers loopback and the
addresses other phones use, and is reused until it nears its end or the
machine's address changes. The pairing address says https when the
encrypted socket is what a phone would meet.
"""

import asyncio
import ipaddress
import socket
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from easyagent import phone as phone_mod
from easyagent import phonetls
from easyagent.access import lan_ip
from easyagent.phone import LanPort, PhoneBook, status_payload
from easyagent.phonetls import _CACHE, ensure, ssl_context


@pytest.fixture(autouse=True)
def _clear_tls_cache():
    saved = dict(_CACHE)
    _CACHE.clear()
    yield
    _CACHE.clear()
    _CACHE.update(saved)


def _certificate(cert_path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(cert_path.read_bytes())


def _write_cert(tmp_path: Path, days_left: int) -> None:
    directory = phonetls.tls_dir(tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "EasyAgent")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=days_left))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address(ip)) for ip in {"127.0.0.1", lan_ip() or ""} - {""}]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    (directory / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )


def test_cert_covers_loopback_and_the_lan_address(tmp_path):
    cert_path = ensure(tmp_path)
    assert cert_path is not None
    assert cert_path.is_file()
    assert cert_path.with_name("key.pem").is_file()
    cert = _certificate(cert_path)
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    ips = {str(ip) for ip in names.get_values_for_type(x509.IPAddress)}
    assert "127.0.0.1" in ips
    assert lan_ip() in ips
    assert socket.gethostname().lower() in {n.lower() for n in names.get_values_for_type(x509.DNSName)}
    constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert constraints.ca is True
    assert constraints.path_length == 0
    assert cert.not_valid_after_utc - cert.not_valid_before_utc <= timedelta(days=397)


def test_second_call_reuses_the_same_cert(tmp_path):
    first = ensure(tmp_path)
    before = first.read_bytes()
    second = ensure(tmp_path)
    assert second.read_bytes() == before


def test_cert_near_its_end_is_replaced(tmp_path):
    _write_cert(tmp_path, days_left=5)
    stale = (phonetls.tls_dir(tmp_path) / "cert.pem").read_bytes()
    cert_path = ensure(tmp_path)
    fresh = cert_path.read_bytes()
    assert fresh != stale
    cert = _certificate(cert_path)
    assert cert.not_valid_after_utc > datetime.now(timezone.utc) + timedelta(days=300)


def test_cert_without_the_current_address_is_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(phonetls, "lan_ip", lambda: "192.168.9.9")
    assert ensure(tmp_path) is not None
    before = (phonetls.tls_dir(tmp_path) / "cert.pem").read_bytes()
    monkeypatch.setattr(phonetls, "lan_ip", lambda: "192.168.9.10")
    cert_path = ensure(tmp_path)
    assert cert_path.read_bytes() != before
    cert = _certificate(cert_path)
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "192.168.9.10" in {str(ip) for ip in names.get_values_for_type(x509.IPAddress)}


def test_ssl_context_is_cached_until_the_files_change(tmp_path):
    first = ssl_context(tmp_path)
    assert isinstance(first, ssl.SSLContext)
    assert ssl_context(tmp_path) is first
    (phonetls.tls_dir(tmp_path) / "cert.pem").write_bytes(b"broken\n")
    assert ssl_context(tmp_path) is not first


def test_pair_url_scheme_follows_the_tls_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(phone_mod, "lan_ip", lambda: "192.168.1.40")
    book = PhoneBook(tmp_path)
    book.set_enabled(True)
    assert book.pair_url(44721).startswith("http://")
    assert book.pair_url(44721, tls=True).startswith("https://")


def test_status_payload_agrees_with_the_pair_url_scheme(tmp_path, monkeypatch):
    monkeypatch.setattr(phone_mod, "lan_ip", lambda: "192.168.1.40")
    book = PhoneBook(tmp_path)
    book.set_enabled(True)
    plain = status_payload(book, 44721, listening=True)
    secure = status_payload(book, 44721, listening=True, tls=True)
    assert plain["pair_url"].startswith("http://")
    assert secure["pair_url"].startswith("https://")
    assert secure["pair_url"].split("://", 1)[1] == plain["pair_url"].split("://", 1)[1]


class _FakeListener:
    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


def _fake_server():
    config = type("Config", (), {"host": "127.0.0.1"})()
    return type("Server", (), {"config": config, "servers": []})()


def test_apply_serves_tls_and_falls_back_when_the_cert_fails(tmp_path, monkeypatch):
    seen = []

    async def fake_beside(server, host, port, ssl_ctx=None):
        seen.append(ssl_ctx)
        return _FakeListener()

    monkeypatch.setattr(phone_mod, "_listen_beside", fake_beside)
    monkeypatch.setattr(phone_mod, "lan_ip", lambda: "192.168.1.40")
    server = _fake_server()
    lan = LanPort(tmp_path)
    assert asyncio.run(lan.apply(server, 44721, True)) is True
    assert isinstance(seen[-1], ssl.SSLContext)
    assert lan.tls is True

    def broken(root):
        raise RuntimeError("no cryptography here")

    monkeypatch.setattr(phonetls, "ssl_context", broken)
    assert asyncio.run(lan.apply(server, 44721, True)) is True
    assert seen[-1] is None
    assert lan.tls is False
