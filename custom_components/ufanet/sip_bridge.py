"""Bridge a SIP monitor call to the browser WebRTC session."""

from __future__ import annotations

import asyncio
import logging
from fractions import Fraction
from typing import Any

from .sip import ALAW_SILENCE, SipMonitor, decode_alaw, encode_alaw

_LOGGER = logging.getLogger(__name__)


async def bridge_call(
    monitor: SipMonitor,
    rtsp_url: str,
    offer_sdp: str,
    send_message: Any,
) -> bool:
    """Answer a browser offer with the SIP call audio and the camera picture.

    Returns False when aiortc is not installed, so the camera can fall back
    to picture-only WebRTC.
    """
    try:
        import av  # noqa: PLC0415
        from aiortc import RTCPeerConnection, RTCSessionDescription  # noqa: PLC0415
        from aiortc.contrib.media import MediaPlayer  # noqa: PLC0415
        from aiortc.mediastreams import AudioStreamTrack  # noqa: PLC0415
    except ImportError:
        _LOGGER.warning("aiortc is not installed; the SIP microphone stays local")
        return False

    from homeassistant.components.camera import (  # noqa: PLC0415
        WebRTCAnswer,
        WebRTCError,
    )

    class _Mic(AudioStreamTrack):
        """Panel audio from the SIP call."""

        def __init__(self) -> None:
            super().__init__()
            self._pts = 0

        async def recv(self) -> av.AudioFrame:
            call = monitor.call
            payload = ALAW_SILENCE
            if call is not None:
                try:
                    payload = await asyncio.wait_for(call.incoming.get(), 0.04)
                except TimeoutError:
                    payload = ALAW_SILENCE
            pcm = decode_alaw(payload[:160].ljust(160, b"\xd5"))
            frame = av.AudioFrame(format="s16", layout="mono", samples=160)
            frame.planes[0].update(pcm)
            frame.sample_rate = 8000
            frame.pts = self._pts
            frame.time_base = Fraction(1, 8000)
            self._pts += 160
            return frame

    pc = RTCPeerConnection()
    player = None
    try:
        player = MediaPlayer(rtsp_url, format="rtsp", options={"rtsp_transport": "tcp"})
    except Exception:  # noqa: BLE001
        _LOGGER.debug("RTSP video for the SIP call did not start", exc_info=True)
    if player and player.video:
        pc.addTrack(player.video)
    pc.addTrack(_Mic())

    @pc.on("track")
    def _on_track(track: Any) -> None:
        if track.kind == "audio":
            asyncio.get_running_loop().create_task(_pump_mic(track, monitor))

    try:
        await pc.setRemoteDescription(RTCSessionDescription(offer_sdp, "offer"))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("SIP WebRTC bridge failed", exc_info=True)
        send_message(WebRTCError("webrtc_offer_failed", str(err)))
        await pc.close()
        return True

    monitor.peer = pc  # type: ignore[attr-defined]
    description = pc.localDescription
    send_message(WebRTCAnswer(description.sdp if description else ""))
    return True


async def _pump_mic(track: Any, monitor: SipMonitor) -> None:
    """Send the browser microphone to the panel as PCMA."""
    try:
        import av  # noqa: PLC0415
    except ImportError:
        return
    resampler = av.AudioResampler(format="s16", layout="mono", rate=8000)
    while True:
        try:
            frame = await track.recv()
        except Exception:  # noqa: BLE001
            return
        for item in resampler.resample(frame):
            pcm = bytes(item.planes[0])
            monitor.send_pcma(encode_alaw(pcm))
