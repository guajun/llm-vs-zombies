"""Offline tests for the #111 boundary lifecycle report (stage C).

The pure-snapshot scenarios model the behaviours issue #111 stage C asks for
(same slot different generation, create+remove inside one boundary, delayed
recycle, unknown removal, initial residue, multi-change boundaries). The
integration test drives the real strict reader end to end; no game is loaded.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from llm_vs_zombies import audit_compare, lifecycle_report  # noqa: E402


def entity(slot, generation, state, *, hp=100, disappeared=0, at_wave=0, type_=16):
    identity = (generation << 16) | slot
    return {"id": identity, "slot": slot, "generation": generation,
            "raw": {"type": type_, "state": state, "hp": hp, "armor1": 0, "armor2": 0,
                    "disappeared": disappeared, "at_wave": at_wave}}


def pool(*entries):
    return {str(entry["slot"]): entry for entry in entries}


def snapshot(zombies, seq=0, tick=0):
    return {"coordinate": {"seq": seq, "kind": "pre_step",
                           "version": {"epoch": 1, "tick": tick, "revision": 0}},
            "zombies": zombies, "problems": []}


def analyze(snapshots):
    return lifecycle_report.analyze_snapshots(snapshots)


class SnapshotScenarioTests(unittest.TestCase):
    def test_confirmed_death_then_delayed_release(self):
        report = analyze([
            snapshot(pool(entity(1, 1, 0)), tick=0),
            snapshot(pool(entity(1, 1, 0)), tick=1),
            snapshot(pool(entity(1, 1, 2)), tick=2),   # ash death stage
            snapshot(pool(entity(1, 1, 2)), tick=3),
            snapshot(pool(), tick=4),                  # slot released later
        ])
        death = report["facts"]["first_confirmed_death"]
        self.assertTrue(death["confirmed"])
        self.assertEqual(death["stage"], "ash")
        self.assertEqual(death["entity"], {"id": 0x10001, "slot": 1, "generation": 1})
        self.assertEqual(death["before"]["state"], 0)
        removal = report["facts"]["first_removal"]
        self.assertEqual(removal["classification"], "release_after_confirmed_death")
        self.assertEqual(report["facts"]["unknown_removals"]["count"], 0)
        self.assertFalse(report["first_kill"]["proven"])
        self.assertEqual(report["capture_level"]["death"], "unavailable")
        self.assertEqual(report["capture_level"]["unimplemented_fact_classes"],
                         ["confirmed_death_stage", "removal_unclassified", "slot_recycle"])

    def test_unknown_removal_is_reported_and_kill_stays_unproven(self):
        report = analyze([
            snapshot(pool(entity(2, 1, 0)), tick=0),
            snapshot(pool(entity(2, 1, 0, disappeared=1)), tick=1),
            snapshot(pool(), tick=2),
        ])
        self.assertFalse(report["facts"]["first_confirmed_death"].get("confirmed", False))
        removal = report["facts"]["first_removal"]
        self.assertEqual(removal["classification"], "unknown_removal")
        self.assertEqual(report["facts"]["disappeared_without_confirmed_death"]["count"], 1)
        self.assertIn("no call-internal ordering", " ".join(report["first_kill"]["reasons"]))
        self.assertEqual(report["first_kill"]["gate"], "unverified")

    def test_same_slot_different_generation_is_not_merged(self):
        report = analyze([
            snapshot(pool(entity(3, 1, 0)), tick=0),
            snapshot(pool(entity(3, 2, 0)), tick=1),
        ])
        reuse = report["facts"]["slot_reuse"]
        self.assertEqual(reuse["count"], 1)
        self.assertEqual(reuse["first"]["previous_identity"], (1 << 16) | 3)
        self.assertEqual(reuse["first"]["entity"]["generation"], 2)
        # The first identity leaving the pool is an unknown removal; the new
        # generation is a fresh entity with no observed death.
        self.assertEqual(report["facts"]["first_removal"]["classification"], "unknown_removal")

    def test_initial_residue_does_not_claim_the_first_removal(self):
        report = analyze([
            snapshot(pool(entity(4, 1, 0, disappeared=1, at_wave=-2)), tick=0),
            snapshot(pool(entity(5, 1, 0)), tick=1),    # real window entity
            snapshot(pool(entity(5, 1, 0, disappeared=1)), tick=2),
            snapshot(pool(), tick=3),
        ])
        residues = report["facts"]["initial_residue"]
        self.assertTrue(any(item["classification"] == "initial_residue_release" for item in residues))
        self.assertEqual(report["facts"]["first_removal"]["entity"]["id"], (1 << 16) | 5)

    def test_create_and_remove_between_boundaries_is_unobserved(self):
        report = analyze([
            snapshot(pool(), tick=0),
            snapshot(pool(entity(6, 1, 0)), tick=1),
            snapshot(pool(), tick=2),
        ])
        removal = report["facts"]["first_removal"]
        self.assertEqual(removal["classification"], "unknown_removal")
        self.assertEqual(removal["entity"]["id"], (1 << 16) | 6)
        self.assertIn("exact_spawn_hook=false", " ".join(report["first_kill"]["reasons"]))

    def test_all_death_stages_and_multiple_changes_in_one_boundary(self):
        report = analyze([
            snapshot(pool(entity(1, 1, 0), entity(2, 1, 0), entity(3, 1, 0)), tick=0),
            snapshot(pool(entity(1, 1, 1), entity(2, 1, 2), entity(3, 1, 3)), tick=1),
        ])
        stages = sorted(item["stage"] for item in report["facts"]["death_stage_observations"])
        self.assertEqual(stages, ["ash", "falling", "mower_death_stage"])
        self.assertEqual(len(report["facts"]["first_confirmed_death"]["coordinates"]), 1)
        # Multiple changes share one boundary coordinate; no ordering is claimed.
        boundary = report["facts"]["death_stage_observations"][0]["coordinates"][0]["version"]
        self.assertTrue(all(item["coordinates"][0]["version"] == boundary
                            for item in report["facts"]["death_stage_observations"]))

    def test_death_without_live_predecessor_is_not_confirmed(self):
        report = analyze([
            snapshot(pool(entity(7, 1, 0)), tick=0),
            snapshot(pool(entity(7, 1, 0, at_wave=-2)), tick=1),
            snapshot(pool(entity(7, 1, 2)), tick=2),
        ])
        observation = report["facts"]["death_stage_observations"][0]
        self.assertFalse(observation["confirmed"])
        self.assertIn("verified live predecessor", observation["reason"])
        self.assertFalse(report["facts"]["first_confirmed_death"].get("confirmed", False))


def build_audit(directory: Path, states: list[dict]):
    """Write a minimal valid lvz.audit.v1 directory for the strict reader."""
    directory.mkdir(parents=True)
    manifest = {"schema": audit_compare.SCHEMA, "target": "synthetic-report-fixture",
                "loaded_signatures_match": True}
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    checks, deltas = [], []
    for index, state in enumerate(states):
        value = dict(state)
        value["schema"] = audit_compare.SCHEMA
        tick, revision = divmod(index, 2)
        kind = "pre_step" if revision == 0 else "post_step"
        version = {"epoch": 1, "tick": tick + (1 if kind == "post_step" else 0), "revision": 0}
        payload = {"request_id": "a"}
        if kind == "post_step":
            payload["native_tick_delta"] = 1
        envelope = {"schema": audit_compare.SCHEMA, "seq": index, "kind": kind,
                    "version": version, "payload": payload}
        checks.append(dict(envelope, digests=audit_compare.digests(value)))
        if index == 0:
            deltas.append(dict(envelope, initial=value))
        else:
            deltas.append(dict(envelope, patch=[{"op": "replace", "path": "", "value": value}]))
    (directory / "checksums.jsonl").write_text("".join(json.dumps(row) + "\n" for row in checks), encoding="utf-8")
    (directory / "state-deltas.jsonl").write_text("".join(json.dumps(row) + "\n" for row in deltas), encoding="utf-8")
    last = checks[-1]["version"]
    (directory / "events.jsonl").write_text(
        json.dumps({"schema": audit_compare.SCHEMA, "seq": len(states), "kind": "recording_closed",
                    "version": last, "payload": {}}) + "\n", encoding="utf-8")


def zombie_state(slots):
    used = max(slots) + 1 if slots else 0
    return {"zombies": {"used": used, "capacity": 1024, "count": len(slots), "free_head": 0,
                        "next_key": 0,
                        "slots": {str(slot): {"id_or_free_next": identity,
                                              "fields": {"00000024": 16, "00000028": state,
                                                         "000000c8": hp, "000000d0": 0,
                                                         "000000dc": 0, "000000ec": disappeared,
                                                         "0000006c": at_wave}}
                                  for slot, (identity, state, hp, disappeared, at_wave) in slots.items()}}}


class AuditIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.audit = Path(self._temp.name) / "audit"

    def test_report_reads_a_real_audit_directory(self):
        first = zombie_state({1: ((1 << 16) | 1, 0, 100, 0, 0)})
        second = zombie_state({1: ((1 << 16) | 1, 2, 100, 0, 0)})
        third = zombie_state({})
        build_audit(self.audit, [first, first, second, second, third, third])
        report = lifecycle_report.report_for_run(self.audit, lifecycle=False)
        self.assertEqual(report["schema"], lifecycle_report.ANALYSIS_SCHEMA)
        self.assertEqual(report["window"]["boundaries"], 6)
        self.assertTrue(report["facts"]["first_confirmed_death"]["confirmed"])
        self.assertEqual(report["facts"]["first_confirmed_death"]["stage"], "ash")
        self.assertEqual(report["facts"]["first_removal"]["classification"], "release_after_confirmed_death")
        self.assertFalse(report["first_kill"]["proven"])

    def test_cli_writes_json_and_markdown(self):
        first = zombie_state({2: ((1 << 16) | 2, 0, 100, 0, 0)})
        build_audit(self.audit, [first, first])
        out = Path(self._temp.name) / "report.json"
        markdown = Path(self._temp.name) / "report.md"
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "issue111_lifecycle_report.py"),
                                 str(self.audit), "--out", str(out), "--markdown", str(markdown)],
                                capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(out.read_text(encoding="utf-8"))
        self.assertFalse(report["first_kill"]["proven"])
        self.assertIn("#111 lifecycle boundary report", markdown.read_text(encoding="utf-8"))

    def test_unreadable_audit_is_a_contract_error(self):
        self.audit.mkdir()
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "issue111_lifecycle_report.py"),
                                 str(self.audit)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
