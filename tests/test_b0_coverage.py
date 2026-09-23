"""Two-way recipe completeness: holes, dangling claims and the laundering rules."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from llm_vs_zombies import b0_coverage as cov
from llm_vs_zombies import b0_normalization as b0
from llm_vs_zombies.audit_compare import EvidenceError, read_json

STATE = {
    "schema": "lvz.audit.v1",
    "rng": {"schema": "lvz.rng.v1", "target": "t", "complete_game_rng": 0, "instances": {"global_mt": 1}},
    "fp_environment": {"x87_control": 0x027f, "mxcsr_control": 0x1f80},
    "draw_schedule": {"mode": "deterministic_draw_schedule_v1", "warm_frames": 1, "step_frames": 0},
    "app": {"game_mode": 13, "ui": 3, "mj_clock": 2048},
    "sound_effects": {"mode": "m", "calls": 0, "app_update_count": 2048, "channels": [0, 0]},
    "board": {"00005568": 1, "0000556c": 2, "00005594": 3, "00005598": 4, "0000559c": 5,
              "000055a0": 6, "00006000": 7},
    "seeds": {"count": 10, "slots": [{"00000008": 1}, {"00000008": 2}]},
    "challenge": None,
    "reanimations": {"valid": True, "nodes": {"a#0": {"state": {"time": 0}}}},
    "particle_shake": {"mode": "deterministic_particle_shake_v1", "controlled_calls": 0, "controlled_digest": 1},
    "zombies": {"slots": {"0": {"fields": {"0000002c": 1}}}},
    "plants": {"slots": {"0": {"fields": {"0000002c": 2}}}},
    "projectiles": {"slots": {}},
    "coins": {"slots": {"0": {"id_or_free_next": 1}}},
    "mowers": {"slots": {"0": {"fields": {"0000002c": 3}}}},
    "grid_items": {"slots": {"0": {"fields": {"0000002c": 4}}}},
}
RECIPE = {"b0_normalization": {"configuration": copy.deepcopy(b0.SPEC),
                               "entries": [{"field": b0.APP_UPDATE_FIELD, "target": 2048, "reason": "declared"},
                                           {"field": b0.MJ_CLOCK_FIELD, "target": 2048, "reason": "declared"}]}}
MANIFEST = {"coverage": {"complete_game_state": False,
                         "uncovered": ["UI/input state", "particle/effect pools",
                                       "Challenge state except completed rounds", "PottedPlant coin specification",
                                       "grid motion trails", "time-source interception",
                                       "RNG calls/instances on other threads",
                                       "MT instances on the stack between boundaries",
                                       "unreferenced animations and attachment graphs"],
                         "reanimations": {"uncovered": ["unreferenced animation semantics", "attachment effect graphs",
                                                        "image/font/text override identity", "particle/trail pools",
                                                        "reanimation allocations entirely between sampled boundaries"]}}}


class LeafEnumerationTests(unittest.TestCase):
    def test_pointer_expansion_matches_the_world_comparison_convention(self):
        self.assertEqual(sorted(cov.leaves({"a": {"b": [1, {"c": 2}]}, "d": {}, "e": []})),
                         ["/a/b/0", "/a/b/1/c", "/d", "/e"])
        self.assertEqual(sorted(cov.leaves({"~x": 1, "a/b": 2})), ["/a~1b", "/~0x"])
        self.assertEqual(list(cov.leaves(7)), [""])

    def test_pattern_matching_is_single_segment_with_prefix_coverage(self):
        self.assertTrue(cov.matches("/a/b/c", "/a/*"))
        self.assertTrue(cov.matches("/a/b/c", "/a/b/c"))
        self.assertFalse(cov.matches("/a/bc", "/a/b"))
        self.assertFalse(cov.matches("/a", "/a/b"))
        self.assertTrue(cov.matches("/zombies/slots/0/fields/0000002c", "/zombies/*"))


class CoverageTests(unittest.TestCase):
    def claims(self, **overrides):
        value = json.loads(json.dumps(cov.DEFAULT_CLAIMS))
        value.update(overrides)
        return value

    def test_default_claims_are_citation_backed_and_the_documented_reading_passes(self):
        # The repo-tracked alternative reading of the proposal classifies the
        # whole Board as recorded; the built-in default is stricter.
        proposal = read_json(Path(__file__).resolve().parents[1] / "determinism/b0-claims-proposal.json")
        report = cov.check(STATE, cov.claims_for(RECIPE, MANIFEST, proposal))
        self.assertTrue(report["coverage_complete"], report["holes"])
        self.assertEqual(report["holes"]["count"], 0)
        self.assertEqual(report["dangling"]["count"], 0)
        self.assertEqual(report["classes"]["normalization"]["leaves"], 2)
        self.assertEqual(report["runtime_declared_boundaries"], sorted(
            set(MANIFEST["coverage"]["uncovered"]) | set(MANIFEST["coverage"]["reanimations"]["uncovered"])))

    def test_every_unclaimed_leaf_is_reported_as_a_hole(self):
        report = cov.check(STATE, cov.claims_for(RECIPE, MANIFEST))
        self.assertFalse(report["coverage_complete"])
        self.assertEqual(report["holes"]["sample"], ["/board/00006000"])
        self.assertEqual(report["holes"]["by_top_level"], {"/board": 1})
        self.assertEqual(report["dangling"]["count"], 0)
        with self.assertRaisesRegex(EvidenceError, r"holes U\\C=1"):
            cov.require_complete(report)

    def test_a_claim_that_lands_on_nothing_is_dangling(self):
        report = cov.check(STATE, self.claims(recorded=[{"pattern": "/board/0000dead",
                                                         "source": "typo"}]))
        self.assertEqual(report["dangling"]["count"], 1)
        self.assertEqual(report["dangling"]["sample"], ["/board/0000dead"])
        self.assertFalse(report["coverage_complete"])

    def test_optional_structures_may_claim_nothing(self):
        report = cov.check(STATE, self.claims(recorded=[{"pattern": "/challenge/completed_rounds",
                                                         "source": "optional", "required": False}]))
        self.assertEqual(report["dangling"]["count"], 0)

    def test_derived_claims_must_name_their_implementation(self):
        with self.assertRaisesRegex(EvidenceError, "implementation"):
            cov.validate_claims(self.claims(derived=[{"pattern": "/reanimations/*",
                                                      "source": "unbacked"}]))

    def test_uncovered_claims_must_cite_the_runtime_boundary_list(self):
        value = self.claims(uncovered=[{"pattern": "/board/*", "manifest": "invented boundary",
                                        "source": "runtime manifest"}])
        with self.assertRaisesRegex(EvidenceError, "invented boundary"):
            cov.claims_for(RECIPE, MANIFEST, value)
        # A boundary claim that overlaps the table is the laundering case.
        value = self.claims(uncovered=[{"pattern": b0.APP_UPDATE_FIELD, "manifest": "grid motion trails",
                                        "source": "runtime manifest"}])
        report = cov.check(STATE, cov.claims_for(RECIPE, MANIFEST, value))
        self.assertIn({"kind": "normalized_field_marked_uncovered", "field": b0.APP_UPDATE_FIELD},
                      report["conflicts"])
        self.assertFalse(report["coverage_complete"])

    def test_a_normalization_field_must_be_exactly_one_integer_leaf(self):
        missing = self.claims(recorded=[{"pattern": "/sound_effects/*", "source": "sound state"}])
        state = copy.deepcopy(STATE)
        state["sound_effects"]["app_update_count"] = "2048"
        report = cov.check(state, cov.claims_for(RECIPE, MANIFEST, missing))
        self.assertEqual([item["kind"] for item in report["conflicts"]],
                         ["normalization_target_is_not_one_integer_leaf"])
        self.assertEqual(report["conflicts"][0]["field"], b0.APP_UPDATE_FIELD)

    def test_legacy_recipes_project_their_two_blocks_into_the_table_class(self):
        from llm_vs_zombies import app_update_anchor, mj_clock_anchor
        recipe = {"app_update_anchor": {"configuration": copy.deepcopy(app_update_anchor.SPEC),
                                        "app_update_count": 1307},
                  "mj_clock_anchor": {"configuration": copy.deepcopy(mj_clock_anchor.SPEC),
                                      "mj_clock": 1340}}
        # The two legacy readers validate their own blocks; coverage only needs
        # the (field, target) projection, which is exactly what identity uses.
        self.assertEqual(cov.projection(recipe), [["/sound_effects/app_update_count", 1307], ["/app/mj_clock", 1340]])

    def test_identity_merge_check_uses_the_projection_only(self):
        left = copy.deepcopy(RECIPE)
        right = copy.deepcopy(RECIPE)
        right["b0_normalization"]["entries"][0]["reason"] = "different wording"
        self.assertTrue(cov.merge_check(left, right)["equal"])
        right["b0_normalization"]["entries"][0]["target"] = 2049
        check = cov.merge_check(left, right)
        self.assertFalse(check["equal"])
        self.assertEqual(check["left"], [["/sound_effects/app_update_count", 2048], ["/app/mj_clock", 2048]])
        self.assertEqual(check["right"], [["/sound_effects/app_update_count", 2049], ["/app/mj_clock", 2048]])

    def test_report_for_run_reads_a_sealed_run_without_starting_a_game(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "run"
            (run / "audit").mkdir(parents=True)
            (run / "observations").mkdir()
            (run / "initialization-recipe.json").write_text(json.dumps(RECIPE), encoding="utf-8")
            (run / "audit/manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
            (run / "observations/initial-audit.json").write_text(
                json.dumps({"state": STATE, "version": {"epoch": 3, "tick": 0, "revision": 7}}), encoding="utf-8")
            (run / "observations/initial.json").write_text(
                json.dumps({"version": {"epoch": 3, "tick": 0, "revision": 7}}), encoding="utf-8")
            proposal = read_json(Path(__file__).resolve().parents[1] / "determinism/b0-claims-proposal.json")
            report = cov.report_for_run(run, claims=proposal)
            self.assertTrue(report["coverage_complete"])
            self.assertEqual(report["b0_version"], {"epoch": 3, "tick": 0, "revision": 7})
            self.assertEqual(report["normalization_projection"],
                             [["/sound_effects/app_update_count", 2048], ["/app/mj_clock", 2048]])
            self.assertEqual(cov.main([str(run), "--claims",
                                       str(Path(__file__).resolve().parents[1] / "determinism/b0-claims-proposal.json")]), 0)


if __name__ == "__main__":
    unittest.main()
