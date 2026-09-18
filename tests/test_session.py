import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from llm_vs_zombies.repl import RecordedConsole
from llm_vs_zombies.session import SessionTrace


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "session.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def records(self):
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def test_append_resume_retains_original_bytes_and_sequence(self):
        with SessionTrace(self.path) as trace:
            trace.emit("first", {"value": "两仪"})
        original = self.path.read_bytes()
        with SessionTrace(self.path) as trace:
            trace.emit("second", {"value": 2})
        self.assertTrue(self.path.read_bytes().startswith(original))
        self.assertEqual([r["seq"] for r in self.records()], [0, 1])

    def test_refuses_simultaneous_writer_and_partial_tail(self):
        with SessionTrace(self.path) as trace:
            trace.emit("first", {})
            with self.assertRaises(FileExistsError):
                SessionTrace(self.path)
        with self.path.open("ab") as out:
            out.write(b'{"schema":')
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            SessionTrace(self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(self.path.with_name(self.path.name + ".lock").exists())

    def test_serialization_error_does_not_corrupt_existing_records(self):
        with SessionTrace(self.path) as trace:
            trace.emit("first", {})
            with self.assertRaises(ValueError):
                trace.emit("invalid", float("nan"))
            trace.emit("last", {})
        self.assertEqual([r["seq"] for r in self.records()], [0, 1])

    def test_repl_keeps_variables_and_records_code_output_and_exception(self):
        with SessionTrace(self.path) as trace:
            console = RecordedConsole(None, trace)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertTrue(console.execute_cell("value = 40\ndef inc(n):\n    return n + 2\n"))
                self.assertTrue(console.execute_cell("value = inc(value)\nprint(value)"))
                self.assertFalse(console.execute_cell("raise RuntimeError('record this failure')"))
                self.assertFalse(console.execute_cell("if ???"))
            self.assertEqual(console.locals["value"], 42)
        events = self.records()
        self.assertEqual(len([r for r in events if r["kind"] == "cell"]), 4)
        exceptions = [r for r in events if r["kind"] == "cell_exception"]
        self.assertEqual(len(exceptions), 2)
        self.assertIn("record this failure", exceptions[0]["data"]["traceback"])
        self.assertTrue(any(r["kind"] == "cell_output" and r["data"]["text"] == "42" for r in events))
        self.assertEqual([r["data"]["ok"] for r in events if r["kind"] == "cell_complete"], [True, True, False, False])

    def test_multiline_interactive_input_records_one_completed_cell(self):
        with SessionTrace(self.path) as trace:
            console = RecordedConsole(None, trace)
            self.assertTrue(console.push("def answer():"))
            self.assertTrue(console.push("    return 42"))
            self.assertFalse(console.push(""))
            self.assertEqual(console.locals["answer"](), 42)
        self.assertEqual(len([r for r in self.records() if r["kind"] == "cell"]), 1)


if __name__ == "__main__":
    unittest.main()
