"""SIP monitor client for a Ufanet intercom panel.

The panel calls the account from «Подключение SIP мониторов».
Audio is G.711 PCMA. Registration is refreshed before it expires.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

_PT_PCMA = 8
_FRAME = 160
ALAW_SILENCE = bytes([0xD5]) * _FRAME


def md5_hex(text: str) -> str:
    """Return the MD5 hex digest used by SIP digest auth."""
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()


def digest_response(
    username: str,
    password: str,
    realm: str,
    nonce: str,
    method: str,
    uri: str,
) -> str:
    """Build the SIP Authorization response hash."""
    ha1 = md5_hex(f"{username}:{realm}:{password}")
    ha2 = md5_hex(f"{method}:{uri}")
    return md5_hex(f"{ha1}:{nonce}:{ha2}")


def parse_auth_challenge(header: str) -> dict[str, str]:
    """Parse a WWW-Authenticate or Proxy-Authenticate header."""
    parts: dict[str, str] = {}
    chunks = header.replace("Digest", "").split(",")
    for key, value in (item.split("=", 1) for item in chunks if "=" in item):
        parts[key.strip()] = value.strip().strip('"')
    return parts


def parse_headers(message: str) -> tuple[str, dict[str, str], str]:
    """Split a SIP message into start line, headers and body."""
    head, _, body = message.partition("\r\n\r\n")
    if not head:
        head, _, body = message.partition("\n\n")
    lines = head.splitlines()
    start = lines[0] if lines else ""
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return start, headers, body


def header_param(value: str, name: str) -> str | None:
    """Return one parameter from a SIP header value."""
    for part in value.split(";"):
        key, sep, raw = part.strip().partition("=")
        if sep and key.lower() == name.lower():
            return raw.strip()
    return None


def sdp_audio(body: str) -> tuple[str | None, int | None, int]:
    """Return remote RTP address, port and PCMA payload type."""
    ip = None
    port = None
    payload = _PT_PCMA
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("c=IN IP4 "):
            ip = line.split()[-1]
        elif line.startswith("m=audio "):
            bits = line.split()
            if len(bits) > 1 and bits[1].isdigit():
                port = int(bits[1])
            for item in bits[3:]:
                if item.isdigit() and int(item) in (8, 0):
                    payload = int(item)
                    if payload == _PT_PCMA:
                        break
    return ip, port, payload


def pcm16_to_alaw(sample: int) -> int:
    """Encode one PCM16 sample as G.711 A-law."""
    amplitude = sample >> 3
    if amplitude >= 0:
        sign = 0x80
    else:
        sign = 0x00
        amplitude = -amplitude - 1
    amplitude = min(amplitude, 0xFFF)
    if amplitude >= 256:
        exponent = 7
        exp_mask = 0x400
        while exp_mask and not (amplitude & exp_mask):
            exponent -= 1
            exp_mask >>= 1
        mantissa = (amplitude >> (exponent + 3)) & 0x0F
        coded = (exponent << 4) | mantissa
    else:
        coded = amplitude >> 4
    return (coded ^ (sign ^ 0x55)) & 0xFF


def alaw_to_pcm16(value: int) -> int:
    """Decode one G.711 A-law byte to PCM16."""
    value ^= 0x55
    sign = value & 0x80
    exponent = (value & 0x70) >> 4
    mantissa = value & 0x0F
    if exponent == 0:
        sample = (mantissa << 4) + 8
    else:
        sample = ((mantissa << 4) + 0x108) << (exponent - 1)
    if sign:
        return sample
    return -sample


def encode_alaw(pcm: bytes) -> bytes:
    """Encode little-endian PCM16 mono to A-law."""
    out = bytearray()
    for index in range(0, len(pcm) - 1, 2):
        sample = int.from_bytes(pcm[index : index + 2], "little", signed=True)
        out.append(pcm16_to_alaw(sample))
    return bytes(out)


def decode_alaw(payload: bytes) -> bytes:
    """Decode A-law to little-endian PCM16 mono."""
    out = bytearray()
    for value in payload:
        out.extend(alaw_to_pcm16(value).to_bytes(2, "little", signed=True))
    return bytes(out)


@dataclass
class SipCall:
    """One answered or ringing panel call."""

    call_id: str
    remote_rtp: tuple[str, int] | None
    payload_type: int
    incoming: asyncio.Queue[bytes]
    outgoing: asyncio.Queue[bytes]


class SipMonitor:
    """Register as a Ufanet SIP monitor and bridge one call."""

    def __init__(
        self,
        username: str,
        password: str,
        host: str,
        port: int,
        on_ring: Callable[[], None],
        on_idle: Callable[[], None] | None = None,
    ) -> None:
        """Store the monitor account."""
        self.username = username
        self.password = password
        self.host = host
        self.port = port
        self._on_ring = on_ring
        self._on_idle = on_idle
        self.registered = False
        self.ringing = False
        self.in_call = False
        self.call: SipCall | None = None
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _SipProtocol | None = None
        self._rtp: asyncio.DatagramTransport | None = None
        self._local_ip = "0.0.0.0"  # noqa: S104
        self._public_ip = ""
        self._public_port = 0
        self._cseq = 1
        self._call_id = secrets.token_hex(8)
        self._tag = secrets.token_hex(4)
        self._register_task: asyncio.Task[None] | None = None
        self._rtp_task: asyncio.Task[None] | None = None
        self._invite: tuple[str, dict[str, str], str] | None = None
        self._answered = False

    async def start(self) -> None:
        """Open sockets and keep the registration alive."""
        loop = asyncio.get_running_loop()
        self._local_ip = await _local_ip(self.host, self.port)
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _SipProtocol(self),
            local_addr=("0.0.0.0", 0),  # noqa: S104
        )
        self._transport = transport
        self._protocol = protocol
        sock = transport.get_extra_info("sockname")
        self._public_port = int(sock[1]) if sock else 0
        rtp, _rtp_protocol = await loop.create_datagram_endpoint(
            lambda: _RtpProtocol(self),
            local_addr=("0.0.0.0", 0),  # noqa: S104
        )
        self._rtp = rtp
        self._register_task = asyncio.create_task(self._register_loop())

    async def stop(self) -> None:
        """Unregister and close sockets."""
        if self._register_task:
            self._register_task.cancel()
        if self._rtp_task:
            self._rtp_task.cancel()
        if self.in_call or self.ringing:
            self._send_bye()
        if self._transport:
            self._transport.close()
        if self._rtp:
            self._rtp.close()
        self.registered = False
        self.in_call = False
        self.ringing = False

    def answer(self) -> None:
        """Pick up the ringing panel."""
        if self._invite and not self._answered:
            self._send_ok()

    def send_pcma(self, payload: bytes) -> None:
        """Queue one 20 ms A-law frame toward the panel."""
        if self.call is None:
            return
        if self.call.outgoing.full():
            _ = self.call.outgoing.get_nowait()
        self.call.outgoing.put_nowait(payload[:_FRAME].ljust(_FRAME, b"\xd5"))

    async def _register_loop(self) -> None:
        while True:
            try:
                self._send_register(auth=None)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("SIP register send failed", exc_info=True)
            await asyncio.sleep(25)

    def _send_register(self, auth: str | None) -> None:
        uri = f"sip:{self.host}"
        contact_host = self._public_ip or self._local_ip
        lines = [
            f"REGISTER {uri} SIP/2.0",
            self._via(),
            f"From: <sip:{self.username}@{self.host}>;tag={self._tag}",
            f"To: <sip:{self.username}@{self.host}>",
            f"Call-ID: {self._call_id}",
            f"CSeq: {self._cseq} REGISTER",
            f"Contact: <sip:{self.username}@{contact_host}:{self._public_port}>",
            "Expires: 30",
            "Max-Forwards: 70",
            "User-Agent: HomeAssistant-Ufanet",
        ]
        if auth:
            lines.append(auth)
        lines.append("Content-Length: 0")
        lines.append("")
        lines.append("")
        self._transmit("\r\n".join(lines))
        self._cseq += 1

    def handle_sip(self, payload: bytes, _remote: tuple[str, int]) -> None:
        """Handle one SIP datagram."""
        text = payload.decode(errors="replace")
        start, headers, body = parse_headers(text)
        via = headers.get("via", "")
        received = header_param(via, "received")
        rport = header_param(via, "rport")
        if received:
            self._public_ip = received
        if rport and rport.isdigit():
            self._public_port = int(rport)
        if start.startswith("SIP/2.0"):
            self._handle_response(start, headers)
            return
        if start.startswith("INVITE "):
            self._handle_invite(start, headers, body)
            return
        if start.startswith("ACK "):
            self.in_call = True
            self.ringing = False
            self._start_rtp()
            return
        if start.startswith(("BYE ", "CANCEL ")):
            self._reply_to(start, headers, "200 OK")
            self._clear_call()

    def _handle_response(self, start: str, headers: dict[str, str]) -> None:
        if " 401 " in start or " 407 " in start:
            challenge = headers.get("www-authenticate") or headers.get(
                "proxy-authenticate", ""
            )
            parsed = parse_auth_challenge(challenge)
            realm = parsed.get("realm", self.host)
            nonce = parsed.get("nonce", "")
            uri = f"sip:{self.host}"
            response = digest_response(
                self.username, self.password, realm, nonce, "REGISTER", uri
            )
            auth = (
                f'Authorization: Digest username="{self.username}", realm="{realm}", '
                f'nonce="{nonce}", uri="{uri}", response="{response}", algorithm=MD5'
            )
            self._send_register(auth)
            return
        if " 200 " in start and "REGISTER" in headers.get("cseq", "").upper():
            self.registered = True

    def _handle_invite(self, start: str, headers: dict[str, str], body: str) -> None:
        call_id = headers.get("call-id", "")
        if (
            self.call is not None
            and self.call.call_id == call_id
            and (self.ringing or self._answered or self.in_call)
        ):
            self._invite = (start, headers, body)
            if self._answered or self.in_call:
                self._send_ok()
            else:
                self._reply_to(start, headers, "180 Ringing")
            return
        self._answered = False
        self.in_call = False
        self._invite = (start, headers, body)
        ip, port, payload = sdp_audio(body)
        remote = (ip, port) if ip and port else None
        self.call = SipCall(
            call_id=headers.get("call-id", ""),
            remote_rtp=remote,
            payload_type=payload,
            incoming=asyncio.Queue(maxsize=50),
            outgoing=asyncio.Queue(maxsize=50),
        )
        self.ringing = True
        self._reply_to(start, headers, "100 Trying")
        self._reply_to(start, headers, "180 Ringing")
        try:
            self._on_ring()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("SIP ring callback failed", exc_info=True)

    def _send_ok(self) -> None:
        if not self._invite or not self._rtp:
            return
        start, headers, _body = self._invite
        sock = self._rtp.get_extra_info("sockname")
        rtp_port = int(sock[1]) if sock else 0
        ip = self._public_ip or self._local_ip
        sdp = (
            "v=0\r\n"
            f"o=- {int(time.time())} 1 IN IP4 {ip}\r\n"
            "s=ufanet\r\n"
            f"c=IN IP4 {ip}\r\n"
            "t=0 0\r\n"
            f"m=audio {rtp_port} RTP/AVP 8\r\n"
            "a=rtpmap:8 PCMA/8000\r\n"
            "a=ptime:20\r\n"
            "a=sendrecv\r\n"
        )
        self._reply_to(start, headers, "200 OK", sdp, content_type="application/sdp")
        self._answered = True

    def _reply_to(
        self,
        _start: str,
        headers: dict[str, str],
        status: str,
        body: str = "",
        content_type: str | None = None,
    ) -> None:
        to_header = headers.get("to", "")
        if "tag=" not in to_header.lower():
            to_header = f"{to_header};tag={self._tag}"
        lines = [
            f"SIP/2.0 {status}",
            headers.get("via", self._via()),
            headers.get("from", ""),
            to_header,
            f"Call-ID: {headers.get('call-id', self._call_id)}",
            headers.get("cseq", "1 INVITE"),
        ]
        if content_type:
            lines.append(f"Content-Type: {content_type}")
        lines.append(f"Content-Length: {len(body.encode())}")
        lines.append("")
        lines.append(body)
        self._transmit("\r\n".join(lines))

    def _send_bye(self) -> None:
        if not self._invite:
            return
        _start, headers, _body = self._invite
        lines = [
            f"BYE sip:{self.username}@{self.host} SIP/2.0",
            self._via(),
            headers.get("from", ""),
            headers.get("to", ""),
            f"Call-ID: {headers.get('call-id', self._call_id)}",
            "CSeq: 2 BYE",
            "Max-Forwards: 70",
            "Content-Length: 0",
            "",
            "",
        ]
        self._transmit("\r\n".join(lines))

    def _clear_call(self) -> None:
        self.ringing = False
        self.in_call = False
        self._answered = False
        self._invite = None
        self.call = None
        if self._on_idle is not None:
            try:
                self._on_idle()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("SIP idle callback failed", exc_info=True)

    def _via(self) -> str:
        host = self._local_ip
        return (
            f"Via: SIP/2.0/UDP {host}:{self._public_port};rport;branch=z9hG4bK"
            f"{secrets.token_hex(6)}"
        )

    def _transmit(self, message: str) -> None:
        if self._transport:
            self._transport.sendto(message.encode(), (self.host, self.port))

    def _start_rtp(self) -> None:
        if self._rtp_task and not self._rtp_task.done():
            return
        self._rtp_task = asyncio.create_task(self._rtp_loop())

    async def _rtp_loop(self) -> None:
        seq = secrets.randbelow(65535)
        timestamp = secrets.randbelow(100000)
        ssrc = secrets.randbits(32)
        while self.call and self.call.remote_rtp and self._rtp:
            try:
                payload = self.call.outgoing.get_nowait()
            except asyncio.QueueEmpty:
                payload = ALAW_SILENCE
            packet = _rtp_packet(seq, timestamp, ssrc, self.call.payload_type, payload)
            self._rtp.sendto(packet, self.call.remote_rtp)
            seq = (seq + 1) & 0xFFFF
            timestamp = (timestamp + _FRAME) & 0xFFFFFFFF
            await asyncio.sleep(0.02)

    def handle_rtp(self, packet: bytes) -> None:
        """Store one received RTP payload."""
        if self.call is None or len(packet) <= 12:
            return
        payload = packet[12:]
        if self.call.incoming.full():
            _ = self.call.incoming.get_nowait()
        self.call.incoming.put_nowait(payload)


def _rtp_packet(
    seq: int, timestamp: int, ssrc: int, payload_type: int, payload: bytes
) -> bytes:
    header = b"\x80" + bytes([payload_type & 0x7F])
    header += seq.to_bytes(2, "big")
    header += timestamp.to_bytes(4, "big")
    header += ssrc.to_bytes(4, "big")
    return header + payload


async def _local_ip(host: str, port: int) -> str:
    loop = asyncio.get_running_loop()

    def lookup() -> str:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((host, port))
            return str(sock.getsockname()[0])
        except OSError:
            return "127.0.0.1"
        finally:
            sock.close()

    return await loop.run_in_executor(None, lookup)


class _SipProtocol(asyncio.DatagramProtocol):
    """Datagram protocol that forwards SIP to the monitor."""

    def __init__(self, monitor: SipMonitor) -> None:
        self._monitor = monitor

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Forward one datagram."""
        try:
            self._monitor.handle_sip(data, addr)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("SIP packet was ignored", exc_info=True)


class _RtpProtocol(asyncio.DatagramProtocol):
    """Datagram protocol that forwards RTP to the monitor."""

    def __init__(self, monitor: SipMonitor) -> None:
        self._monitor = monitor

    def datagram_received(self, data: bytes, _addr: tuple[str, int]) -> None:
        """Forward one RTP packet."""
        self._monitor.handle_rtp(data)
