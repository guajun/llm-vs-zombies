"""Cache-boundary and legacy negotiation fixtures; not live frame validation."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from llm_vs_zombies.client import Client
from llm_vs_zombies.initialization import DRAW_MODE, LEGACY_DRAW_MODE, apply_recipe
from llm_vs_zombies.recording import CaptureUnavailable, RemoteGameFrameProvider
from llm_vs_zombies.session import SessionTrace
from llm_vs_zombies.video import StreamingVideo, VideoFrame
from test_initialization import PreparationRuntime


class ControlledCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.trace=SessionTrace(self.root/'trace.jsonl')
        self.runtime=PreparationRuntime(); self.client=Client(self.runtime,trace=self.trace)
        apply_recipe(self.client,42)
        self.provider=RemoteGameFrameProvider.from_hello(
            lambda method,params,expect:self.client.request(method,params,expect=expect),self.client.hello_result)

    def tearDown(self):
        self.client.close(); self.trace.close(); self.temp.cleanup()

    def test_repeated_cache_reads_keep_exact_revision_pixels_and_draw_count(self):
        observation=self.client.observation
        before=copy.deepcopy(self.runtime.state)
        a=self.provider.capture(observation); b=self.provider.capture(observation)
        self.assertEqual(a,b); self.assertEqual(self.runtime.state,before)
        self.assertEqual(self.runtime.draws,1)
        self.assertEqual(a.metadata['frame_version'],observation['version'])
        self.assertIs(a.metadata['forced_render'],False)
        self.assertEqual(self.provider.capabilities()['method'],'cached_controlled_engine_frame')

    def test_action_only_invalidates_cache_without_automatic_draw_or_advance(self):
        prior=self.client.observation
        changed=self.client.commit([{'op':'plant','type':8,'row':1,'col':8}],advance_ticks=0)['observation']
        self.assertEqual(prior['version']['tick'],changed['version']['tick'])
        self.assertGreater(changed['version']['revision'],prior['version']['revision'])
        methods=[r['method'] for r in self.runtime.requests]
        with self.assertRaisesRegex(CaptureUnavailable,'frame_cache_stale'):
            self.provider.capture(changed)
        self.assertEqual([r['method'] for r in self.runtime.requests][len(methods):],['capture_frame','observe'])
        self.assertEqual(self.runtime.draws,1)
        self.assertEqual(self.provider.last_observation,changed)

    def test_legacy_provider_keeps_old_label_instead_of_claiming_new_mode(self):
        provider=RemoteGameFrameProvider.from_hello(lambda *_:{},
            {'capabilities':{'capture_frame':True},'game':{}})
        self.assertEqual(provider.capabilities()['mode'],LEGACY_DRAW_MODE)
        self.assertEqual(provider.capabilities()['method'],'runtime_capture_rpc')

    def test_unnegotiated_new_mode_is_rejected_by_provider(self):
        result=self.client.capture_frame()
        provider=RemoteGameFrameProvider(lambda method,*_:result if method=='capture_frame' else self.client.observation,
                                        {'available':True})
        with self.assertRaisesRegex(CaptureUnavailable,'not negotiated'):
            provider.capture(self.client.observation)

    def test_bad_cached_frame_stamp_fails_even_without_client_validation(self):
        result=self.client.capture_frame(); result['frame_version']['revision']-=1
        provider=RemoteGameFrameProvider(lambda method,*_:result if method=='capture_frame' else self.client.observation,
                                        {'available':True,'mode':DRAW_MODE})
        with self.assertRaisesRegex(CaptureUnavailable,'cache does not certify'):
            provider.capture(self.client.observation)

    def test_video_sidecar_preserves_cache_evidence_and_rejects_relabelled_frame(self):
        frame=self.provider.capture(self.client.observation)
        video=StreamingVideo(self.root/'cached.mp4',width=2,height=2,epoch=frame.stamp.epoch,
            source='original_game_frame',pixel_format='bgr24',provider=self.provider.capabilities(),
            ffmpeg='nonexistent-lvz-ffmpeg-binary')
        bad=VideoFrame(frame.stamp,2,2,'bgr24',frame.pixels,frame.source,{**frame.metadata,'forced_render':True})
        with self.assertRaisesRegex(ValueError,'cached-frame evidence'): video.submit(bad)
        video.submit(frame); video.close()
        records=[json.loads(line) for line in video.mapping_path.read_text().splitlines()]
        encoded=next(record for record in records if record['kind']=='frame')
        self.assertEqual(encoded['metadata']['frame_version'],frame.stamp.as_dict())
        self.assertFalse(encoded['metadata']['forced_render'])
        self.assertEqual(json.loads(video.manifest_path.read_text())['provider']['mode'],DRAW_MODE)


if __name__=='__main__': unittest.main()
