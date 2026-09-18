"""Optional streaming video with explicit simulation-tick/frame correspondence.

No screenshots are written per tick. Raw, tightly packed RGB/BGR pixels flow to
FFmpeg's stdin; the sidecar records what each encoded frame actually represents.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from .initialization import DRAW_MODE


class VideoError(RuntimeError):
    pass


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True)
class TickStamp:
    epoch: int
    tick: int
    revision: int = 0

    def __post_init__(self):
        for name in ("epoch", "tick", "revision"):
            _integer(getattr(self, name), name)

    @classmethod
    def from_observation(cls, observation: dict) -> "TickStamp":
        version = observation["version"]
        return cls(version["epoch"], version["tick"], version["revision"])

    def as_dict(self) -> dict:
        return {"epoch": self.epoch, "tick": self.tick, "revision": self.revision}


@dataclass(frozen=True)
class VideoFrame:
    stamp: TickStamp
    width: int
    height: int
    pixel_format: str
    pixels: bytes
    source: str
    metadata: dict = field(default_factory=dict)


class _PipeWriter:
    """A persistent writer provides a deadline even if FFmpeg stops consuming."""
    def __init__(self, process: subprocess.Popen):
        self.process = process
        self.tasks: queue.Queue = queue.Queue(maxsize=1)
        self.thread = threading.Thread(target=self._run, name="lvz-video-pipe", daemon=True)
        self.thread.start()

    def _run(self):
        while True:
            task = self.tasks.get()
            if task is None:
                return
            pixels, done, errors = task
            try:
                remaining = memoryview(pixels)
                while remaining:
                    count = self.process.stdin.write(remaining)
                    if not count:
                        raise BrokenPipeError("FFmpeg accepted zero bytes")
                    remaining = remaining[count:]
                self.process.stdin.flush()
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()

    def write(self, pixels: bytes, timeout: float):
        done, errors = threading.Event(), []
        self.tasks.put((pixels, done, errors), timeout=timeout)
        if not done.wait(timeout):
            self.process.kill()
            raise VideoError("FFmpeg input timed out; encoder was terminated")
        if errors:
            raise VideoError(f"FFmpeg input failed: {errors[0]}") from errors[0]

    def close(self, timeout: float):
        self.tasks.put(None, timeout=timeout)
        self.thread.join(timeout)
        if self.thread.is_alive():
            self.process.kill()
            raise VideoError("FFmpeg writer did not stop")


class StreamingVideo:
    """One epoch and one fixed tick grid per video; wall-clock time is irrelevant.

    A missing grid point is explicitly held/blank, not removed from the timeline.
    `optional=True` permits missing FFmpeg, but encoder failures always raise.
    Use TickRecorder to isolate optional video failures from the experiment.
    """
    SOURCES = {"original_game_frame", "state_visualization", "synthetic_fixture"}

    def __init__(self, output: Path, *, width: int, height: int, epoch: int,
                 start_tick: int = 0, tick_stride: int = 10, ticks_per_second: int = 100,
                 slowdown: str | int | Fraction = 1, pixel_format: str = "rgb24",
                 source: str, ffmpeg: str | None = None, optional: bool = True,
                 timeout: float = 15.0, max_gap_frames: int = 10000,
                 provider: dict | None = None):
        self.output = Path(output)
        if self.output.suffix.lower() != ".mp4":
            raise ValueError("streaming output must use the .mp4 extension")
        for name, value, minimum in (("width", width, 2), ("height", height, 2),
                                    ("epoch", epoch, 0), ("start_tick", start_tick, 0),
                                    ("tick_stride", tick_stride, 1), ("ticks_per_second", ticks_per_second, 1),
                                    ("max_gap_frames", max_gap_frames, 0)):
            _integer(value, name, minimum)
        if width % 2 or height % 2 or width > 8192 or height > 8192:
            raise ValueError("H.264 dimensions must be even and at most 8192")
        if pixel_format not in ("rgb24", "bgr24") or source not in self.SOURCES:
            raise ValueError("unsupported pixel format or source label")
        self.slowdown = Fraction(slowdown)
        if self.slowdown <= 0 or timeout <= 0:
            raise ValueError("slowdown and timeout must be positive")
        self.fps = Fraction(ticks_per_second, tick_stride) / self.slowdown
        if self.fps < Fraction(1, 1000) or self.fps > 1000:
            raise ValueError("video frame rate must be between 0.001 and 1000")
        self.width, self.height, self.epoch = width, height, epoch
        self.start_tick, self.next_tick = start_tick, start_tick
        self.tick_stride, self.ticks_per_second = tick_stride, ticks_per_second
        self.pixel_format, self.source, self.timeout = pixel_format, source, timeout
        self.max_gap_frames, self.provider = max_gap_frames, provider or {}
        self.frame_count = self.dropped_count = self.rejected_count = self.content_count = 0
        self.previous: VideoFrame | None = None
        self.status, self.error, self.closed = "starting", None, False
        self.process = self.writer = self.stderr = None
        self.mapping_path = self.output.with_suffix(".frames.jsonl")
        self.manifest_path = self.output.with_suffix(".video.json")
        self.stderr_path = self.output.with_suffix(".ffmpeg.log")
        self.output.parent.mkdir(parents=True, exist_ok=True)
        for path in (self.output, self.mapping_path, self.manifest_path, self.stderr_path):
            if path.exists():
                raise FileExistsError(f"refusing to overwrite video artifact: {path}")
        self.mapping = self.mapping_path.open("x", encoding="utf-8")
        binary = shutil.which(ffmpeg or "ffmpeg")
        self.command = []
        if binary is None:
            self.status, self.error = "disabled", "ffmpeg_not_found"
            self._save_manifest()
            if not optional:
                self.mapping.close(); self.closed = True
                raise VideoError("FFmpeg is unavailable")
            return
        self.command = [binary, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
                        "-f", "rawvideo", "-pixel_format", pixel_format,
                        "-video_size", f"{width}x{height}", "-framerate", str(self.fps),
                        "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                        "-metadata", f"comment=LVZ source={source}; timing=game_tick; see .frames.jsonl",
                        "-f", "mp4", str(self.output)]
        try:
            self.stderr = self.stderr_path.open("xb")
            self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                                            stdout=subprocess.DEVNULL, stderr=self.stderr, bufsize=0,
                                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self.writer = _PipeWriter(self.process)
            self.status = "recording"
        except OSError as exc:
            self.status, self.error = "failed", str(exc)
            self.mapping.close(); self.closed = True
            if self.stderr: self.stderr.close()
            self._save_manifest()
            raise VideoError(f"Cannot start FFmpeg: {exc}") from exc
        self._save_manifest()

    def _event(self, value: dict):
        self.mapping.write(json.dumps(value, sort_keys=True, allow_nan=False) + "\n")
        self.mapping.flush()

    def _save_manifest(self):
        value = {"schema": 1, "source": self.source,
                 "original_game_pixels": self.source == "original_game_frame" and self.content_count > 0 and self.status != "disabled",
                 "status": self.status, "error": self.error, "provider": self.provider,
                 "width": self.width, "height": self.height, "pixel_format": self.pixel_format,
                 "epoch": self.epoch, "start_tick": self.start_tick,
                 "tick_stride": self.tick_stride, "ticks_per_second": self.ticks_per_second,
                 "timing_basis": "game_tick", "video_fps": str(self.fps), "slowdown": str(self.slowdown),
                 "frame_count": self.frame_count, "dropped_frames": self.dropped_count,
                 "content_frames": self.content_count,
                 "encoded_frames": 0 if self.status == "disabled" else self.frame_count,
                 "rejected_frames": self.rejected_count, "next_tick": self.next_tick,
                 "duration_seconds": str(Fraction(self.frame_count, 1) / self.fps),
                 "gap_policy": "hold_previous_or_blank", "command": self.command,
                 "mapping": self.mapping_path.name, "stderr": self.stderr_path.name}
        self.manifest_path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")

    def _check_stamp(self, stamp: TickStamp):
        if self.closed or self.status == "failed":
            raise VideoError("video writer is closed or failed")
        if stamp.epoch != self.epoch:
            raise ValueError("epoch changed; open a separate video for the new epoch")
        if stamp.tick < self.next_tick or (stamp.tick - self.start_tick) % self.tick_stride:
            raise ValueError("tick must be increasing and aligned to the configured grid")
        if (stamp.tick - self.next_tick) // self.tick_stride > self.max_gap_frames:
            raise ValueError("gap exceeds max_gap_frames; open a new video segment")

    def _encode(self, stamp: TickStamp, pixels: bytes, kind: str, *, reason: str | None = None,
                source_stamp: TickStamp | None = None, metadata: dict | None = None):
        record = {"kind": "frame", "frame_index": self.frame_count, "stamp": stamp.as_dict(),
                  "source_stamp": source_stamp.as_dict() if source_stamp else None,
                  "content": kind, "reason": reason, "encoded": self.status != "disabled",
                  "video_pts_seconds": str(Fraction(self.frame_count, 1) / self.fps),
                  "simulation_seconds": str(Fraction(stamp.tick - self.start_tick, self.ticks_per_second)),
                  "pixel_sha256": hashlib.sha256(pixels).hexdigest(), "metadata": metadata or {}}
        try:
            if self.writer:
                if self.process.poll() is not None:
                    raise VideoError(f"FFmpeg exited early with code {self.process.returncode}")
                self.writer.write(pixels, self.timeout)
        except (OSError, VideoError, queue.Full) as exc:
            self.status, self.error = "failed", str(exc)
            self._event({"kind": "encoder_failure", "at": stamp.as_dict(), "message": str(exc)})
            self._save_manifest()
            raise VideoError(str(exc)) from exc
        self._event(record)
        self.frame_count += 1
        self.next_tick = stamp.tick + self.tick_stride

    def _drop_one(self, stamp: TickStamp, reason: str):
        if self.previous:
            pixels, kind, origin = self.previous.pixels, "held", self.previous.stamp
        else:
            pixels, kind, origin = bytes(self.width * self.height * 3), "blank", None
        self._encode(stamp, pixels, kind, reason=reason, source_stamp=origin)
        self.dropped_count += 1

    def _fill_gap(self, stamp: TickStamp):
        while self.next_tick < stamp.tick:
            self._drop_one(TickStamp(self.epoch, self.next_tick), "missing_grid_tick")

    def submit(self, frame: VideoFrame):
        try:
            self._check_stamp(frame.stamp)
            if (frame.width, frame.height, frame.pixel_format, frame.source) != (self.width, self.height, self.pixel_format, self.source):
                raise ValueError("frame geometry, pixel format or source changed")
            if not isinstance(frame.pixels, bytes) or len(frame.pixels) != self.width * self.height * 3:
                raise ValueError("frame must contain exactly width * height * 3 packed bytes")
            if self.provider.get("mode") == DRAW_MODE:
                if (frame.metadata.get("mode") != DRAW_MODE
                        or frame.metadata.get("method") != "cached_controlled_engine_frame"
                        or frame.metadata.get("forced_render") is not False
                        or frame.metadata.get("frame_version") != frame.stamp.as_dict()):
                    raise ValueError("controlled video requires exact cached-frame evidence")
            # Validate metadata before sending irreversible bytes to the encoder.
            json.dumps(frame.metadata, allow_nan=False)
        except (ValueError, TypeError) as exc:
            self.rejected_count += 1
            if not self.closed:
                self._event({"kind": "frame_rejected", "stamp": frame.stamp.as_dict(), "message": str(exc)})
            raise
        self._fill_gap(frame.stamp)
        self._encode(frame.stamp, frame.pixels, "captured" if self.source == "original_game_frame" else "generated",
                     source_stamp=frame.stamp, metadata=frame.metadata)
        self.previous = frame
        self.content_count += 1

    def drop(self, stamp: TickStamp, reason: str):
        self._check_stamp(stamp)
        if not isinstance(reason, str) or not reason:
            raise ValueError("drop reason must be a non-empty string")
        self._fill_gap(stamp)
        self._drop_one(stamp, reason)

    def close(self) -> dict:
        if self.closed:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        failure = None
        try:
            if self.writer:
                self.writer.close(self.timeout)
                self.process.stdin.close()
                try:
                    code = self.process.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired as exc:
                    self.process.kill(); self.process.wait(timeout=self.timeout)
                    raise VideoError("FFmpeg finalization timed out") from exc
                if code != 0:
                    raise VideoError(f"FFmpeg exited with code {code}; see {self.stderr_path.name}")
            if self.status == "recording":
                self.status = ("empty" if not self.frame_count else "no_content_frames" if not self.content_count
                               else "partial" if self.dropped_count else "complete")
        except (OSError, VideoError, queue.Full) as exc:
            failure = exc
            self.status, self.error = "failed", str(exc)
            self._event({"kind": "encoder_failure", "during": "close", "message": str(exc)})
            if self.process and self.process.poll() is None:
                self.process.kill(); self.process.wait(timeout=self.timeout)
        finally:
            self.closed = True
            self.mapping.close()
            if self.stderr: self.stderr.close()
            self._save_manifest()
        if failure:
            raise VideoError(str(failure)) from failure
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        try:
            self.close()
        except VideoError:
            if exc_type is None:
                raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tick-mapped state-visualization video (not original game footage)")
    parser.add_argument("run", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--segment", type=int, default=0)
    parser.add_argument("--stride", type=int, default=10)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=400)
    parser.add_argument("--slowdown", default="1")
    parser.add_argument("--ffmpeg")
    args = parser.parse_args(argv)
    from .recording import render_run
    try:
        result = render_run(args.run, args.output, segment=args.segment, tick_stride=args.stride,
                            width=args.width, height=args.height, slowdown=args.slowdown, ffmpeg=args.ffmpeg)
    except (ValueError, OSError, VideoError) as exc:
        parser.exit(1, f"video: {exc}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
