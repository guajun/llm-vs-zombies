from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from llm_vs_zombies.video import StreamingVideo, TickStamp, VideoError, VideoFrame
from llm_vs_zombies.recording import (CaptureUnavailable, RemoteGameFrameProvider,
                                     StateVisualizationProvider, TickRecorder, render_run)


FFMPEG, FFPROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")


def frame(tick=0, *, pixel_format="rgb24", epoch=1, color=(210, 25, 15), source="synthetic_fixture"):
    return VideoFrame(TickStamp(epoch, tick), 64, 48, pixel_format, bytes(color) * 64 * 48, source)


def probe(path: Path):
    result = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-count_frames",
                             "-show_entries", "stream=width,height,nb_read_frames,r_frame_rate,duration",
                             "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)["streams"][0]


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def video(self, name="test.mp4", **kwargs):
        return StreamingVideo(self.root / name, width=64, height=48, epoch=1,
                              source=kwargs.pop("source", "synthetic_fixture"), **kwargs)

    def test_optional_missing_encoder_retains_mapping(self):
        video = self.video(ffmpeg="nonexistent-lvz-ffmpeg-binary")
        video.submit(frame(0)); video.submit(frame(20))
        result = video.close()
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["encoded_frames"], 0)
        self.assertEqual(result["frame_count"], 3)
        self.assertFalse(video.output.exists())
        mapping = [json.loads(line) for line in video.mapping_path.read_text().splitlines()]
        self.assertEqual([r["stamp"]["tick"] for r in mapping], [0, 10, 20])
        self.assertEqual(mapping[1]["content"], "held")
        self.assertTrue(all(not r["encoded"] for r in mapping))

    def test_order_size_epoch_and_source_are_rejected_before_advancement(self):
        with self.video(ffmpeg="nonexistent-lvz-ffmpeg-binary") as video:
            video.submit(frame(0))
            for invalid in (frame(0), frame(5), frame(10, epoch=2), frame(10, source="original_game_frame"),
                            VideoFrame(TickStamp(1, 10), 64, 48, "rgb24", b"short", "synthetic_fixture")):
                with self.assertRaises(ValueError): video.submit(invalid)
            self.assertEqual(video.next_tick, 10)
            video.submit(frame(10))
        result = json.loads(video.manifest_path.read_text())
        self.assertEqual(result["rejected_frames"], 5)
        self.assertEqual(result["frame_count"], 2)

    def test_no_overwrite_and_gap_limit(self):
        video = self.video(ffmpeg="nonexistent-lvz-ffmpeg-binary", max_gap_frames=2)
        with self.assertRaises(ValueError): video.submit(frame(30))
        video.close()
        with self.assertRaises(FileExistsError): self.video(ffmpeg="nonexistent-lvz-ffmpeg-binary")

    @unittest.skipUnless(FFMPEG and FFPROBE, "real FFmpeg/ffprobe are not installed")
    def test_real_stream_timing_and_held_frame_count(self):
        video = self.video(ffmpeg=FFMPEG)
        video.submit(frame(0)); video.submit(frame(10)); video.submit(frame(30))
        report = video.close()
        info = probe(video.output)
        self.assertEqual((info["width"], info["height"], info["nb_read_frames"]), (64, 48, "4"))
        self.assertEqual(info["r_frame_rate"], "10/1")
        self.assertAlmostEqual(float(info["duration"]), .4, places=4)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["dropped_frames"], 1)

    @unittest.skipUnless(FFMPEG and FFPROBE, "real FFmpeg/ffprobe are not installed")
    def test_bgr_channels_and_slowdown_decode_correctly(self):
        video = self.video(pixel_format="bgr24", tick_stride=25, slowdown=2, ffmpeg=FFMPEG)
        for tick in (0, 25, 50): video.submit(frame(tick, pixel_format="bgr24", color=(5, 10, 230)))
        video.close()
        info = probe(video.output)
        self.assertEqual(info["r_frame_rate"], "2/1")
        self.assertAlmostEqual(float(info["duration"]), 1.5, places=4)
        pixels = subprocess.run([FFMPEG, "-v", "error", "-i", str(video.output), "-frames:v", "1",
                                 "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                                capture_output=True, check=True).stdout
        self.assertEqual(len(pixels), 64 * 48 * 3)
        self.assertGreater(pixels[0], 200); self.assertLess(pixels[2], 30)

    @unittest.skipUnless(FFMPEG, "real FFmpeg is not installed")
    def test_encoder_crash_is_recorded_and_not_claimed_complete(self):
        video = self.video(ffmpeg=FFMPEG)
        video.submit(frame(0)); video.process.kill(); video.process.wait(timeout=5)
        with self.assertRaises(VideoError): video.submit(frame(10))
        with self.assertRaises(VideoError): video.close()
        manifest = json.loads(video.manifest_path.read_text())
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("encoder_failure", video.mapping_path.read_text())

    def test_remote_frame_strides_orientation_version_and_black_rejection(self):
        expected = {"epoch": 1, "tick": 20, "revision": 3}
        result = {"source": "original_game_frame", "capture_ok": True, "version": expected,
                  "width": 2, "height": 2, "pixel_format": "bgr24", "row_stride": 8,
                  "origin": "bottom_left", "pixels_base64": base64.b64encode(b"abcdefXXghijklXX").decode()}
        calls = []
        def rpc(method, params, expect):
            calls.append((method, params, expect)); return result
        provider = RemoteGameFrameProvider(rpc, {"available": True, "hidden_window_support": "unverified"})
        captured = provider.capture({"version": expected})
        self.assertEqual(captured.pixels, b"ghijklabcdef")
        self.assertEqual(calls[0][0], "capture_frame")
        self.assertEqual(calls[0][2], expected)
        result["version"] = {**expected, "tick": 21}
        with self.assertRaises(CaptureUnavailable): provider.capture({"version": expected})
        result["version"] = expected; result["pixels_base64"] = base64.b64encode(bytes(16)).decode()
        with self.assertRaisesRegex(CaptureUnavailable, "black"): provider.capture({"version": expected})
        def unavailable_rpc(*_): raise RuntimeError("unsupported_method")
        provider = RemoteGameFrameProvider(unavailable_rpc, {"available": True})
        with self.assertRaisesRegex(CaptureUnavailable, "unsupported_method"):
            provider.capture({"version": expected})

    def test_capture_failure_refreshes_observation_without_retrying_render(self):
        expected = {"epoch": 1, "tick": 20, "revision": 3}
        updated = {"version": {**expected, "revision": 4}, "game_clock": 20}
        calls = []
        def rpc(method, params, expect):
            calls.append(method)
            if method == "capture_frame":
                return {"capture_ok": False, "reason": "capture_changed_game_state", "version": updated["version"]}
            self.assertEqual(method, "observe")
            self.assertIsNone(expect)
            return updated
        provider = RemoteGameFrameProvider(rpc, {"available": True})
        with self.assertRaisesRegex(CaptureUnavailable, "capture_changed_game_state"):
            provider.capture({"version": expected})
        self.assertEqual(calls, ["capture_frame", "observe"])
        self.assertEqual(provider.last_observation, updated)

    def test_tick_recorder_preserves_experiment_when_video_unavailable(self):
        provider = RemoteGameFrameProvider(lambda *_: self.fail("unavailable provider must not make RPC"), {"available": False})
        video = self.video(ffmpeg="nonexistent-lvz-ffmpeg-binary", source="original_game_frame")
        with TickRecorder(provider, video, self.root / "recording.jsonl") as recorder:
            report = recorder.record({"version": {"epoch": 1, "tick": 0, "revision": 0}})
            self.assertEqual(report["status"], "dropped")
        self.assertIn("unavailable", (self.root / "recording.jsonl").read_text())
        self.assertEqual(json.loads(video.manifest_path.read_text())["content_frames"], 0)

    @unittest.skipUnless(FFMPEG and FFPROBE, "real FFmpeg/ffprobe are not installed")
    def test_offline_run_is_labelled_visualization_and_holds_sparse_tail(self):
        (self.root / "manifest.json").write_text(json.dumps({"run_id": "fixture"}))
        observations = []
        for seq, tick in enumerate((0, 25)):
            observations.append({"schema_version": 1, "run_id": "fixture", "seq": seq, "segment": 0,
                                 "tick": tick, "phase": "observation", "kind": "state",
                                 "payload": {"scene": 3, "wave": 1, "sun": 500, "plants": [], "zombies": []}})
        (self.root / "events.jsonl").write_text("".join(json.dumps(v) + "\n" for v in observations))
        output = self.root / "diagram.mp4"
        result = render_run(self.root, output, width=320, height=240, ffmpeg=FFMPEG)
        self.assertEqual(result["source"], "state_visualization")
        self.assertFalse(result["original_game_pixels"])
        self.assertEqual(probe(output)["nb_read_frames"], "3")
        self.assertEqual(result["dropped_frames"], 2)
        rendered = StateVisualizationProvider(320, 240).capture({**observations[0]["payload"],
                                                               "version": {"epoch": 0, "tick": 0, "revision": 0}})
        self.assertEqual(len(rendered.pixels), 320 * 240 * 3)
        self.assertIn("no native sprites", rendered.metadata["limitations"])


if __name__ == "__main__":
    unittest.main()
