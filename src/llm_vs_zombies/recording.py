"""Frame providers and optional recording, independent of the experiment logger."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Callable, Protocol

from .video import StreamingVideo, TickStamp, VideoError, VideoFrame


class CaptureUnavailable(RuntimeError):
    """The provider cannot certify an actual frame at the requested boundary."""


class FrameProvider(Protocol):
    def capabilities(self) -> dict: ...
    def capture(self, observation: dict) -> VideoFrame: ...


class RemoteGameFrameProvider:
    """Adapter for an explicitly negotiated native `capture_frame` RPC.

    The supplied callable receives (method, params, expect). No capture RPC is
    assumed to exist merely because a window handle is available.
    """
    def __init__(self, request: Callable, capability: dict):
        self.request, self.capability = request, dict(capability)
        self.last_observation: dict | None = None

    def capabilities(self) -> dict:
        return {**self.capability, "source": "original_game_frame",
                "available": self.capability.get("available") is True,
                "method": "runtime_capture_rpc"}

    def capture(self, observation: dict) -> VideoFrame:
        self.last_observation = None
        try:
            return self._capture(observation)
        except CaptureUnavailable:
            # Rendering can reveal a changed boundary, and a failed RPC may
            # have completed before transport failure. Refresh if the same
            # connection remains usable; never retry the capture automatically.
            if self.capabilities()["available"]:
                try:
                    refreshed = self.request("observe", {}, None)
                    TickStamp.from_observation(refreshed)
                    self.last_observation = refreshed
                except Exception:
                    pass  # A broken connection requires an explicit reconnect.
            raise

    def _capture(self, observation: dict) -> VideoFrame:
        if not self.capabilities()["available"]:
            raise CaptureUnavailable("native original-frame capture capability is unavailable")
        stamp = TickStamp.from_observation(observation)
        try:
            result = self.request("capture_frame", {"format": "bgr24"}, stamp.as_dict())
        except Exception as exc:
            raise CaptureUnavailable(f"capture RPC failed: {exc}") from exc
        if result.get("source") != "original_game_frame" or result.get("capture_ok") is not True:
            raise CaptureUnavailable(result.get("reason", "runtime did not certify a captured frame"))
        actual = TickStamp(**result["version"])
        if actual != stamp:
            raise CaptureUnavailable("capture version differs from the completed observation")
        width, height = result["width"], result["height"]
        if type(width) is not int or type(height) is not int or not 1 <= width <= 8192 or not 1 <= height <= 8192:
            raise CaptureUnavailable("invalid capture geometry")
        pixel_format = result.get("pixel_format")
        if pixel_format not in ("rgb24", "bgr24"):
            raise CaptureUnavailable("unsupported capture pixel format")
        stride = result.get("row_stride", width * 3)
        if type(stride) is not int or stride < width * 3 or stride > width * 3 + 4096:
            raise CaptureUnavailable("invalid capture row stride")
        try:
            raw = base64.b64decode(result["pixels_base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise CaptureUnavailable("invalid capture base64") from exc
        if len(raw) != stride * height:
            raise CaptureUnavailable("capture buffer length does not match geometry")
        origin = result.get("origin", "top_left")
        if origin not in ("top_left", "bottom_left"):
            raise CaptureUnavailable("unknown capture orientation")
        order = range(height) if origin == "top_left" else range(height - 1, -1, -1)
        pixels = b"".join(raw[row * stride:row * stride + width * 3] for row in order)
        if not any(pixels):
            raise CaptureUnavailable("uniform black frame; hidden-window capture may be unsupported")
        return VideoFrame(stamp, width, height, pixel_format, pixels, "original_game_frame",
                          {"method": result.get("method", "unspecified"),
                           **{key: result[key] for key in ("forced_render", "used_3d", "known_rng_unchanged",
                              "game_clock_before", "game_clock_after") if key in result},
                           "hidden_window_support": self.capability.get("hidden_window_support", "unverified")})


# Small public-domain-style bitmap glyph definitions authored for this project.
_GLYPHS = {
    "A":"010/101/111/101/101", "B":"110/101/110/101/110", "C":"011/100/100/100/011",
    "D":"110/101/101/101/110", "E":"111/100/110/100/111", "F":"111/100/110/100/100",
    "G":"011/100/101/101/011", "H":"101/101/111/101/101", "I":"111/010/010/010/111",
    "J":"001/001/001/101/010", "K":"101/101/110/101/101", "L":"100/100/100/100/111",
    "M":"101/111/111/101/101", "N":"101/111/111/111/101", "O":"010/101/101/101/010",
    "P":"110/101/110/100/100", "Q":"010/101/101/111/011", "R":"110/101/110/101/101",
    "S":"011/100/010/001/110", "T":"111/010/010/010/010", "U":"101/101/101/101/111",
    "V":"101/101/101/101/010", "W":"101/101/111/111/101", "X":"101/101/010/101/101",
    "Y":"101/101/010/010/010", "Z":"111/001/010/100/111",
    "0":"111/101/101/101/111", "1":"010/110/010/010/111", "2":"110/001/010/100/111",
    "3":"110/001/010/001/110", "4":"101/101/111/001/001", "5":"111/100/110/001/110",
    "6":"011/100/111/101/111", "7":"111/001/010/010/010", "8":"111/101/111/101/111",
    "9":"111/101/111/001/110", "-":"000/000/111/000/000", ":":"000/010/000/010/000",
}


class StateVisualizationProvider:
    """Pure-stdlib diagram renderer. It never claims to reproduce original pixels."""
    def __init__(self, width: int = 640, height: int = 400):
        if type(width) is not int or type(height) is not int or width < 240 or height < 180:
            raise ValueError("state visualization needs at least 240 x 180 pixels")
        self.width, self.height = width, height

    def capabilities(self) -> dict:
        return {"source": "state_visualization", "available": True, "hidden_window_support": "not_applicable",
                "method": "structured_state_diagram", "original_game_pixels": False}

    def capture(self, observation: dict) -> VideoFrame:
        stamp = TickStamp.from_observation(observation)
        width, height = self.width, self.height
        image = bytearray(bytes((20, 25, 32)) * width * height)

        def rect(x, y, w, h, color):
            left, top = max(0, int(x)), max(0, int(y))
            right, bottom = min(width, int(x + w)), min(height, int(y + h))
            if right <= left or bottom <= top: return
            strip = bytes(color) * (right - left)
            for row in range(top, bottom):
                image[(row * width + left) * 3:(row * width + right) * 3] = strip

        def text(value, x, y, color=(238, 239, 230), scale=2):
            for character in str(value).upper():
                glyph = _GLYPHS.get(character)
                if glyph:
                    for row, bits in enumerate(glyph.split("/")):
                        for col, bit in enumerate(bits):
                            if bit == "1": rect(x + col * scale, y + row * scale, scale, scale, color)
                x += 4 * scale

        rect(0, 0, width, 31, (91, 35, 36))
        text("STATE VISUALIZATION", 9, 4)
        text("NOT ORIGINAL GAME FOOTAGE", 9, 18, scale=1)
        text(f"TICK {stamp.tick}  WAVE {observation.get('wave', 0)}  SUN {observation.get('sun', 0)}", 9, 39, scale=1)
        left, top, grid_w, grid_h = 25, 60, width - 45, height - 76
        rows = 6 if observation.get("scene") in (2, 3) else 5
        cell_w, cell_h = grid_w / 9, grid_h / rows
        for row in range(rows):
            for col in range(9):
                pool = rows == 6 and row in (2, 3)
                color = (35, 83, 105) if pool else ((41, 74, 49) if (row + col) % 2 else (46, 82, 55))
                rect(left + col * cell_w, top + row * cell_h, cell_w - 1, cell_h - 1, color)
        for plant in observation.get("plants", []):
            row, col = plant.get("row", 0) - 1, plant.get("col", 0) - 1
            if not 0 <= row < rows or not 0 <= col < 9: continue
            x, y = left + (col + .2) * cell_w, top + (row + .2) * cell_h
            rect(x, y, cell_w * .58, cell_h * .58, (129, 179, 72))
            text(plant.get("type", 0), x + 3, y + 3, (20, 35, 20), scale=1)
            hp = max(0, min(1, plant.get("hp", 0) / 300))
            rect(x, y + cell_h * .63, cell_w * .58 * hp, 3, (185, 225, 112))
        for zombie in observation.get("zombies", []):
            row = zombie.get("row", 0) - 1
            if not 0 <= row < rows: continue
            x = left + ((float(zombie.get("x", 0)) - 40) / 760) * grid_w
            y = top + (row + .3) * cell_h
            rect(x - 5, y, 13, cell_h * .5, (203, 89, 78))
            text(zombie.get("type", 0), x - 5, y - 7, scale=1)
        return VideoFrame(stamp, width, height, "rgb24", bytes(image), "state_visualization",
                          {"limitations": "diagram of recorded fields; no native sprites, particles, fog or interpolation"})


class TickRecorder:
    """Capture only completed observation boundaries; video failure is optional.

    Construct this separately from the experiment's state/action logger. `record`
    returns a report and never advances the game. Do not call it from a wall-clock
    polling loop and assume those frames represent new simulation ticks.
    """
    def __init__(self, provider: FrameProvider, video: StreamingVideo | None,
                 events_path: Path, *, continue_on_video_error: bool = True):
        self.provider, self.video = provider, video
        self.continue_on_video_error = continue_on_video_error
        self.events = Path(events_path).open("x", encoding="utf-8")
        self.failed, self.closed, self.sequence = False, False, 0
        self._event("provider", capability=provider.capabilities(), video_enabled=video is not None)

    def _event(self, kind: str, **payload):
        self.events.write(json.dumps({"seq": self.sequence, "kind": kind, **payload}, allow_nan=False) + "\n")
        self.events.flush(); self.sequence += 1

    def record(self, observation: dict) -> dict:
        if self.closed: raise VideoError("recording session is closed")
        stamp = TickStamp.from_observation(observation)
        if self.video is None or self.failed:
            report = {"stamp": stamp.as_dict(), "status": "video_disabled"}
            self._event("capture", **report); return report
        # Bad caller ordering is a programming error, not an optional video drop.
        self.video._check_stamp(stamp)
        try:
            if not self.provider.capabilities().get("available", False):
                raise CaptureUnavailable("frame provider is unavailable")
            frame = self.provider.capture(observation)
            if frame.stamp != stamp:
                raise CaptureUnavailable("provider returned a frame from a different game boundary")
            self.video.submit(frame)
            report = {"stamp": stamp.as_dict(), "status": "captured", "source": frame.source}
        except (CaptureUnavailable, ValueError, TypeError, KeyError) as exc:
            try:
                self.video.drop(stamp, str(exc))
            except (VideoError, OSError) as encoding:
                return self._video_failure(stamp, encoding)
            report = {"stamp": stamp.as_dict(), "status": "dropped", "reason": str(exc)}
        except (VideoError, OSError) as exc:
            return self._video_failure(stamp, exc)
        self._event("capture", **report)
        return report

    def _video_failure(self, stamp: TickStamp, exc: BaseException) -> dict:
        self.failed = True
        report = {"stamp": stamp.as_dict(), "status": "video_failed", "reason": str(exc)}
        self._event("capture", **report)
        if not self.continue_on_video_error: raise VideoError(str(exc)) from exc
        return report

    def close(self):
        if self.closed: return
        try:
            if self.video:
                try: self.video.close()
                except VideoError as exc:
                    self.failed = True; self._event("video_close_failed", reason=str(exc))
                    if not self.continue_on_video_error: raise
            self._event("closed", video_failed=self.failed)
        finally:
            self.closed = True; self.events.close()

    def __enter__(self): return self
    def __exit__(self, *_): self.close()


def render_run(run: Path, output: Path, *, segment: int = 0, tick_stride: int = 10,
               width: int = 640, height: int = 400, slowdown=1, ffmpeg: str | None = None) -> dict:
    """Render one recorded segment, holding missing samples and labelling them."""
    from .records import states
    iterator = (record for record in states(Path(run)) if record["segment"] == segment)
    first = next(iterator, None)
    if first is None: raise ValueError(f"no state observations for segment {segment}")
    provider = StateVisualizationProvider(width, height)
    start = first["tick"]
    video = StreamingVideo(output, width=width, height=height, epoch=segment, start_tick=start,
                           tick_stride=tick_stride, slowdown=slowdown, source="state_visualization",
                           ffmpeg=ffmpeg, provider=provider.capabilities())
    from itertools import chain
    last_tick = -1
    try:
        for record in chain((first,), iterator):
            tick = record["tick"]
            if tick <= last_tick:
                raise ValueError("offline state ticks must be strictly increasing within a segment")
            last_tick = tick
            if (tick - start) % tick_stride:
                # Never invent an observation at another tick or resample by wall clock.
                video._event({"kind": "observation_not_on_video_grid", "tick": tick})
                continue
            observation = {**record["payload"], "version": {"epoch": segment, "tick": tick, "revision": 0}}
            video.submit(provider.capture(observation))
        while video.next_tick <= last_tick:
            video.drop(TickStamp(segment, video.next_tick), "no_observation_at_grid_tick")
        return video.close()
    except BaseException as exc:
        if not video.closed:
            video.status, video.error = "failed", f"offline rendering failed: {exc}"
            video._event({"kind": "render_failure", "message": str(exc)})
        try: video.close()
        except VideoError: pass
        raise
