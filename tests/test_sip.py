"""Unit tests for the SIP monitor message helpers."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from types import ModuleType

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_sip() -> ModuleType:
    """Load sip.py without Home Assistant."""
    path = ROOT / "custom_components" / "ufanet" / "sip.py"
    spec = importlib.util.spec_from_file_location("ufanet_sip_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sip = _load_sip()


def test_digest_matches_rfc2617_example_shape() -> None:
    """The response hash is stable for a known challenge."""
    response = sip.digest_response(
        "user", "pass", "realm", "nonce", "REGISTER", "sip:host"
    )
    assert response == sip.md5_hex(
        f"{sip.md5_hex('user:realm:pass')}:nonce:{sip.md5_hex('REGISTER:sip:host')}"
    )


def test_invite_sdp_prefers_pcma() -> None:
    """Ufanet monitors speak PCMA payload 8."""
    body = (
        "v=0\nc=IN IP4 81.30.201.150\nm=audio 40000 RTP/AVP 0 8\na=rtpmap:8 PCMA/8000"
    )
    ip, port, payload = sip.sdp_audio(body)
    assert ip == "81.30.201.150"
    assert port == 40000
    assert payload == 8


def test_retransmitted_invite_rings_once() -> None:
    """A repeated INVITE must not steal the line a second time."""
    rings: list[int] = []
    monitor = sip.SipMonitor(
        "user", "pass", "81.30.201.150", 5401, lambda: rings.append(1)
    )
    monitor._rtp = type(  # noqa: SLF001
        "Rtp",
        (),
        {"get_extra_info": lambda _self, _name: ("0.0.0.0", 40000)},  # noqa: S104
    )()
    monitor._local_ip = "10.0.0.8"  # noqa: SLF001
    body = "v=0\r\nc=IN IP4 81.30.201.150\r\nm=audio 40000 RTP/AVP 8\r\n"
    packet = (
        "INVITE sip:user@81.30.201.150 SIP/2.0\r\n"
        "Via: SIP/2.0/UDP 81.30.201.150:5401;branch=z9hG4bK1\r\n"
        "From: <sip:panel@81.30.201.150>;tag=from\r\n"
        "To: <sip:user@81.30.201.150>\r\n"
        "Call-ID: call-1\r\n"
        "CSeq: 1 INVITE\r\n"
        f"Content-Length: {len(body)}\r\n"
        "\r\n"
        f"{body}"
    ).encode()
    monitor.handle_sip(packet, ("81.30.201.150", 5401))
    monitor.answer()
    monitor.handle_sip(packet, ("81.30.201.150", 5401))
    assert rings == [1]
    assert monitor.ringing is True


def test_alaw_silence_roundtrip_stays_quiet() -> None:
    """Silence bytes survive a PCM conversion."""
    pcm = sip.decode_alaw(sip.ALAW_SILENCE)
    assert len(pcm) == 320
    again = sip.encode_alaw(pcm)
    assert again == sip.ALAW_SILENCE
