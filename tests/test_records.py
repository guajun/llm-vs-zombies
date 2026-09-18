import json
import tempfile
import unittest
from pathlib import Path

from llm_vs_zombies.cli import ROOT, activate, attach, create_run, demo, export
from llm_vs_zombies.records import EventWriter, compare, finish, read_json, validate, write_json


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'replay/viewer').mkdir(parents=True)
        (self.root / 'replay/viewer/template.html').write_text(
            (ROOT / 'replay/viewer/template.html').read_text(encoding='utf-8'), encoding='utf-8')
        self.config = self.root / 'config.json'
        write_json(self.config, {'state_interval_ticks': 10})

    def tearDown(self):
        self.tmp.cleanup()

    def runfile(self, name='a', hp=300):
        run = create_run(self.root, self.config, name)
        with EventWriter(run) as log:
            log.emit('state', 0, 100, {'plants': [{'id': 1, 'hp': hp}]})
            log.emit('segment_end', 0, 100, {})
        return run

    def test_demo_pipeline_and_equal_compare(self):
        a = demo(self.root, self.config, 'a')
        b = demo(self.root, self.config, 'b')
        self.assertTrue(read_json(a/'manifest.json')['synthetic'])
        self.assertEqual(validate(a)['states'], 121)
        self.assertTrue(compare(a,b)['equal'])
        self.assertTrue((a/'exports/review.html').is_file())

    def test_first_divergence(self):
        result = compare(self.runfile('a'),self.runfile('b',hp=299))
        self.assertFalse(result['equal'])
        self.assertEqual(result['at'],(0,100,'observation'))

    def test_partial_line_is_rejected(self):
        run = self.runfile()
        with (run/'events.jsonl').open('ab') as f: f.write(b'{"kind":')
        with self.assertRaisesRegex(ValueError,'incomplete'): validate(run)

    def test_missing_sequence_is_rejected(self):
        run = self.runfile()
        p = run/'events.jsonl'
        p.write_text(p.read_text().replace('"seq":1','"seq":5'))
        with self.assertRaisesRegex(ValueError,'sequence'): validate(run)

    def test_segment_supports_reset_tick(self):
        run = create_run(self.root,self.config,'a')
        with EventWriter(run) as log:
            log.emit('state',0,1000,{})
            log.emit('state',1,0,{})
        self.assertEqual(validate(run)['states'],2)

    def test_backward_tick_same_segment_is_rejected(self):
        run = create_run(self.root,self.config,'a')
        with EventWriter(run) as log:
            log.emit('state',0,1000,{})
            log.emit('state',0,999,{})
        with self.assertRaisesRegex(ValueError,'backwards'): validate(run)

    def test_finalized_file_tampering_is_detected(self):
        run = self.runfile()
        finish(run,'test')
        p=run/'events.jsonl'
        p.write_text(p.read_text().replace('300','299'))
        with self.assertRaisesRegex(ValueError,'checksum'): validate(run)

    def test_export_cannot_inject_script(self):
        run = self.runfile()
        m=read_json(run/'manifest.json');m['note']='</script><script>alert(1)</script>'
        write_json(run/'manifest.json',m)
        html=export(self.root,run).read_text(encoding='utf-8')
        self.assertNotIn('</script><script>alert',html)
        self.assertIn('\\u003c/script>',html)

    def test_refuse_overwrite_and_run_escape(self):
        self.runfile()
        with self.assertRaises(FileExistsError): create_run(self.root,self.config,'a')
        with self.assertRaises(ValueError): create_run(self.root,self.config,'../escape')

    def test_native_lock_prevents_finalization(self):
        run=self.runfile();(run/'capture.lock').touch()
        with self.assertRaisesRegex(ValueError,'capture'): finish(run,'test')

    def test_attachments_are_hashed(self):
        run=self.runfile();source=self.root/'answer.json';source.write_text('{"action":"wait"}')
        attach(run,source,'decisions');finish(run,'test')
        (run/'decisions/answer.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'checksum'): validate(run)

    def test_activate_refuses_existing_capture(self):
        run=self.runfile()
        with self.assertRaises(ValueError): activate(self.root,run)


if __name__=='__main__': unittest.main()
