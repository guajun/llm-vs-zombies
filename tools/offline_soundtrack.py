"""Render a tick-aligned soundtrack from a Foley event stream (N9, issue #37).

The tool is offline: it never starts a game, opens a device or reads a wall
clock. It consumes

* an event stream: ``lvz.audio-event.v1`` JSONL (or ``--from-foley-trace`` to
  read ``audit/foley-events.jsonl`` rows from ``foley_branch_trace_v1``),
* a soundscape: ``lvz.audio-resources.v1`` JSON with sound durations/levels and
  the Foley type flags the audible admission policy needs,

and writes a 16-bit stereo WAV on the game-tick grid plus a manifest that binds
the tick grid, every voice, the union/silence coverage and the payload hash.

    python tools/offline_soundtrack.py build --events events.jsonl \
        --resources soundscape.json --wav track.wav --end-tick 6000
    python tools/offline_soundtrack.py verify --wav track.wav \
        --manifest track.audio.json

``--mux`` attaches the track to an existing video without re-encoding it; the
video manifest is then checked for the same tick grid first.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from llm_vs_zombies import virtual_audio


def build(arguments) -> int:
    soundscape = virtual_audio.load_soundscape(arguments.resources)
    if arguments.from_foley_trace:
        rows = []
        for number, line in enumerate(Path(arguments.events).read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise virtual_audio.AudioModelError(f"trace line {number} is not JSON: {error}") from error
        events = virtual_audio.events_from_foley_trace(rows, soundscape)
    else:
        events = virtual_audio.load_events(arguments.events)
    if not events:
        raise virtual_audio.AudioModelError("the event stream produced no play events")
    track = virtual_audio.render_track(events, soundscape, start_tick=arguments.start_tick,
                                       end_tick=arguments.end_tick, admission=arguments.admission)
    wav = Path(arguments.wav)
    manifest = wav.with_suffix(".audio.json") if arguments.manifest is None else Path(arguments.manifest)
    for path in (wav, manifest):
        if path.exists():
            raise virtual_audio.AudioModelError(f"refusing to overwrite {path}")
    alignment = check_alignment(arguments.video_manifest, track, arguments.mux is not None)
    track.manifest["alignment"] = alignment
    virtual_audio.write_wav(wav, track)
    manifest.write_text(json.dumps(track.manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
                        encoding="utf-8")
    report = {"wav": str(wav), "manifest": str(manifest), "frames": track.frames,
              "seconds": track.manifest["duration_seconds"], "start_tick": track.start_tick,
              "end_tick": track.end_tick, "voices": len(track.manifest["voices"]),
              "dropped": len(track.manifest["dropped"]), "coverage": track.manifest["coverage"],
              "alignment": alignment}
    if arguments.mux is not None:
        muxed = mux(arguments, wav)
        report["mux"] = muxed
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0


def check_alignment(video_manifest: str | None, track, required: bool) -> dict:
    """The audio grid must be the video's tick grid, not a lookalike."""
    if video_manifest is None:
        if required:
            raise virtual_audio.AudioModelError("--mux needs --video-manifest for the tick-grid check")
        return {"checked": False, "reason": "no video manifest supplied"}
    value = json.loads(Path(video_manifest).read_text(encoding="utf-8"))
    if value.get("timing_basis") != "game_tick":
        raise virtual_audio.AudioModelError("the video manifest is not on the game-tick basis")
    if value.get("ticks_per_second") != track.manifest["ticks_per_second"]:
        raise virtual_audio.AudioModelError("video and audio ticks per second differ")
    offset = track.start_tick - int(value.get("start_tick", 0))
    alignment = {"checked": True, "video": str(video_manifest), "video_epoch": value.get("epoch"),
                 "video_start_tick": value.get("start_tick"), "audio_start_tick": track.start_tick,
                 "tick_offset": offset, "offset_seconds": f"{offset / track.manifest['ticks_per_second']:.6f}"}
    if required and offset != 0:
        raise virtual_audio.AudioModelError(f"audio starts {offset} ticks away from the video; align the streams first")
    return alignment


def mux(arguments, wav: Path) -> dict:
    import shutil
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise virtual_audio.AudioModelError("FFmpeg is unavailable; keep the WAV next to the video")
    output = Path(arguments.output)
    if output.exists():
        raise virtual_audio.AudioModelError(f"refusing to overwrite {output}")
    command = [binary, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
               "-i", str(arguments.mux), "-i", str(wav),
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
               "-metadata", f"comment=LVZ soundtrack={wav.name}; timing=game_tick",
               str(output)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise virtual_audio.AudioModelError(f"FFmpeg mux failed: {result.stderr.strip()}")
    return {"output": str(output), "command": command}


def verify(arguments) -> int:
    manifest = json.loads(Path(arguments.manifest).read_text(encoding="utf-8"))
    report = virtual_audio.verify_track(arguments.wav, manifest)
    report["wav"] = str(arguments.wav)
    report["manifest"] = str(arguments.manifest)
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0 if report["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build", help="render a tick-aligned WAV from an event stream")
    builder.add_argument("--events", required=True, help="lvz.audio-event.v1 JSONL file")
    builder.add_argument("--resources", required=True, help="lvz.audio-resources.v1 soundscape JSON file")
    builder.add_argument("--wav", required=True, help="output WAV path (never overwritten)")
    builder.add_argument("--manifest", help="output manifest path (default: <wav>.audio.json)")
    builder.add_argument("--start-tick", type=int, default=0, help="first tick of the track (default 0)")
    builder.add_argument("--end-tick", type=int,
                         help="last tick of the track; required when a looping voice is still open")
    builder.add_argument("--admission", choices=virtual_audio.ADMISSION_POLICIES, default="original",
                         help="audible admission policy (default: original)")
    builder.add_argument("--from-foley-trace", action="store_true",
                         help="read lvz.foley-event.v1 rows instead of lvz.audio-event.v1")
    builder.add_argument("--video-manifest", help="matching .video.json to check the tick grid against")
    builder.add_argument("--mux", help="existing video to attach the track to")
    builder.add_argument("--output", help="muxed output path (required with --mux)")
    verifier = commands.add_parser("verify", help="re-check duration and sample coverage of a track")
    verifier.add_argument("--wav", required=True)
    verifier.add_argument("--manifest", required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "build":
            if arguments.mux is not None and arguments.output is None:
                parser.exit(2, "--output is required with --mux\n")
            return build(arguments)
        return verify(arguments)
    except virtual_audio.AudioModelError as error:
        parser.exit(2, f"{error}\n")
    except (OSError, KeyError, TypeError, ValueError) as error:
        parser.exit(1, f"soundtrack rejected: {error}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
