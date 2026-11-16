"""The firewall copy must name the port actually in use, not a hardcoded 44721."""
from easyagent.phone import PhoneBook, add_firewall_rule, firewall_hint, status_payload


class _Done:
    returncode = 0


def test_hint_names_the_passed_port():
    hint = firewall_hint(44791)
    assert "TCP port 44791" in hint
    assert "44721" not in hint


def test_hint_matches_default_port_when_default():
    assert "TCP port 44721" in firewall_hint(44721)


def test_status_payload_uses_the_real_port(tmp_path):
    phone = PhoneBook(tmp_path)
    phone.set_enabled(True)
    body = status_payload(phone, 44791, listening=True)
    assert "TCP port 44791" in body["firewall"]["hint"]
    assert "44721" not in body["firewall"]["hint"]


def test_firewall_success_detail_names_the_passed_port():
    result = add_firewall_rule(44791, True, platform_name="win32", runner=lambda command: _Done())
    assert result["added"] is True
    assert "TCP 44791" in result["detail"]
    assert "44721" not in result["detail"]
