"""Formal-schema validation for the lifecycle record/receipt contracts.

The repository keeps the formal JSON Schemas next to the native writer. The
test suite has no third-party jsonschema dependency, so this module implements
the small draft-2020-12 subset the two schemas actually use and validates the
real mixed v1/v2 artifact produced by the native fixture.
"""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SCHEMA_DIR = ROOT / "logger" / "schemas"


def load_schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


class SchemaError(ValueError):
    pass


def _resolve(schema: dict, root: dict, path: str) -> dict:
    while "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            raise SchemaError(f"{path}: unsupported $ref {ref}")
        target = root
        for part in ref[2:].split("/"):
            target = target[part]
        schema = target
    return schema


def validate(instance, schema: dict, root: dict | None = None, path: str = "$") -> list[str]:
    root = root if root is not None else schema
    schema = _resolve(schema, root, path)
    problems: list[str] = []
    if "oneOf" in schema:
        matches = 0
        for alternative in schema["oneOf"]:
            if not validate(instance, alternative, root, path):
                matches += 1
        if matches != 1:
            problems.append(f"{path}: oneOf matched {matches} alternatives")
    if "type" in schema:
        types = schema["type"]
        types = types if isinstance(types, list) else [types]
        checks = {
            "object": lambda value: isinstance(value, dict),
            "array": lambda value: isinstance(value, list),
            "string": lambda value: isinstance(value, str),
            "integer": lambda value: type(value) is int,
            "boolean": lambda value: type(value) is bool,
            "null": lambda value: value is None,
        }
        if not any(checks[kind](instance) for kind in types):
            problems.append(f"{path}: expected {types}, found {type(instance).__name__}")
            return problems
    if "const" in schema and instance != schema["const"]:
        problems.append(f"{path}: expected const {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        problems.append(f"{path}: value {instance!r} not in enum {schema['enum']!r}")
    if type(instance) is int and "minimum" in schema and instance < schema["minimum"]:
        problems.append(f"{path}: {instance} below minimum {schema['minimum']}")
    if type(instance) is int and "maximum" in schema and instance > schema["maximum"]:
        problems.append(f"{path}: {instance} above maximum {schema['maximum']}")
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            problems.append(f"{path}: string shorter than minLength {schema['minLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            problems.append(f"{path}: string does not match {schema['pattern']}")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                problems.append(f"{path}: missing required key {key}")
        properties = schema.get("properties", {})
        for key, value in instance.items():
            if key in properties:
                problems += validate(value, properties[key], root, f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                problems.append(f"{path}: unexpected key {key}")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            problems.append(f"{path}: fewer than minItems {schema['minItems']}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            problems.append(f"{path}: more than maxItems {schema['maxItems']}")
        if "items" in schema:
            for index, value in enumerate(instance):
                problems += validate(value, schema["items"], root, f"{path}[{index}]")
    return problems


class FormalSchemaTests(unittest.TestCase):
    def emit_artifact(self) -> Path:
        executable = ROOT / "build" / "determinism_lifecycle_probes.exe"
        if not executable.is_file():
            self.skipTest("native probe fixture is not built")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        target = Path(temp.name) / "artifact"
        result = subprocess.run([str(executable), "--emit-audit", str(target)], capture_output=True,
                                text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        return target

    def test_native_mixed_artifact_matches_formal_schemas(self):
        artifact = self.emit_artifact()
        record_schema = load_schema("lifecycle-record.schema.json")
        problems: list[str] = []
        lines = 0
        for line in (artifact / "lifecycle-events.jsonl").read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            lines += 1
            problems += validate(json.loads(line), record_schema,
                                 path=f"line {lines}")
        self.assertGreater(lines, 2)
        self.assertFalse(problems, problems)
        receipt = json.loads((artifact / "lifecycle-close-receipt.jsonl").read_text(encoding="utf-8"))
        receipt_problems = validate(receipt, load_schema("lifecycle-close-receipt.schema.json"))
        self.assertFalse(receipt_problems, receipt_problems)

    def test_schema_rejects_unknown_v2_field(self):
        record_schema = load_schema("lifecycle-record.schema.json")
        event = {
            "schema": "lvz.lifecycle-event.v2", "kind": "zombie_phase_transition",
            "capture_sequence": 1, "version": None, "version_phase": "uncontrolled_update",
            "engine_call_id": None,
            "entity": {"id": 0x00020001, "slot": 1, "generation": 2},
            "object": {"class": "zombie", "on_board": True, "wave": 0, "board": 0x1234},
            "probe": {"name": "zombie-lifecycle-store", "schema": "lvz.lifecycle-event.v2",
                      "sequence_domain": "lvz.measurement.capture-sequence"},
            "phase": {"site": "phase-mowdown", "before": 0, "after": 3},
            "extra": 1, "complete": True,
        }
        envelope = {"schema": "lvz.lifecycle-record.v1", "file_seq": 0, "run_id": "r",
                    "branch_id": "b", "session_id": 1,
                    "sequence_domain": "lvz.measurement.capture-sequence", "event": event}
        self.assertTrue(validate(envelope, record_schema))


if __name__ == "__main__":
    unittest.main()
