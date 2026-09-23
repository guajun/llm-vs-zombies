"""Offline contract tests for the #51 gargantuar fork scenarios.

No game is launched. The two example scripts are executed through the real
``ScriptStrategy`` console (the production loader) and then stepped against a
synthetic tick stream, so these checks cover the strategy contract, the parameter
constants and the exact A/B difference. They say nothing about how the live engine
answers the spawned giant.
"""
import ast
import importlib.util
import tempfile
import unittest
from pathlib import Path

from llm_vs_zombies.client import plant as client_plant
from llm_vs_zombies.client import spawn as client_spawn
from llm_vs_zombies.evaluation import ScriptStrategy
from llm_vs_zombies.session import SessionTrace

ROOT = Path(__file__).resolve().parents[1]
PATHS = {"a": ROOT / "examples/liangyi_gargantuar_a.py",
         "b": ROOT / "examples/liangyi_gargantuar_b.py"}
BUDGET = 2000


def load(branch):
    spec = importlib.util.spec_from_file_location("gargantuar_" + branch, PATHS[branch])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def code_only(branch):
    """The module source with the fork constant normalised away."""
    tree = ast.parse(PATHS[branch].read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(target, "id", None) == "PUFF_COL"
                                               for target in node.targets):
            node.value = ast.Constant(value="<fork>")
    return ast.dump(tree)


def run(strategy, budget=BUDGET):
    """Drive one decision source with a synthetic tick stream.

    Mirrors the runner contract: ``remaining_ticks`` is the only budget the policy
    sees, an advance never exceeds it, and the loop ends on the tick budget.
    """
    tick, decisions, zeroes, worst = 0, [], 0, 0
    while tick < budget:
        observation = {"version": {"epoch": 1, "tick": tick, "revision": len(decisions)}}
        context = {"seed": 42, "decision_index": len(decisions), "remaining_ticks": budget - tick,
                   "tier": "smoke", "target": "tick_budget"}
        decision = strategy(observation, context)
        if set(decision) != {"actions", "advance_ticks"}:
            raise AssertionError(f"decision keys are {sorted(decision)}")
        if not isinstance(decision["actions"], list) or type(decision["advance_ticks"]) is not int:
            raise AssertionError(f"malformed decision {decision} at tick {tick}")
        if not 0 <= decision["advance_ticks"] <= min(100000, budget - tick):
            raise AssertionError(f"advance beyond the budget at tick {tick}: {decision}")
        decisions.append({"actions": decision["actions"], "advance_ticks": decision["advance_ticks"]})
        tick += decision["advance_ticks"]
        zeroes = zeroes + 1 if decision["advance_ticks"] == 0 else 0
        worst = max(worst, zeroes)
        if len(decisions) > budget + 8:
            raise AssertionError("decision stream never consumes the tick budget")
    return decisions, worst


def ticks_of(decisions):
    tick, ticks = 0, []
    for decision in decisions:
        ticks.append(tick)
        tick += decision["advance_ticks"]
    return ticks, tick


class GargantuarScenarioTests(unittest.TestCase):
    def test_both_scripts_load_through_the_production_loader(self):
        with tempfile.TemporaryDirectory() as directory:
            for branch in sorted(PATHS):
                with self.subTest(branch=branch):
                    with SessionTrace(Path(directory) / f"{branch}.jsonl") as trace:
                        strategy = ScriptStrategy(object(), trace, PATHS[branch], 100)
                        decision = strategy.decide(
                            {"version": {"epoch": 1, "tick": 0, "revision": 0}},
                            {"seed": 42, "decision_index": 0, "remaining_ticks": 500,
                             "tier": "smoke", "target": "tick_budget"})
                    self.assertEqual(set(decision), {"actions", "advance_ticks"})
                    self.assertEqual(decision["advance_ticks"], 0)
                    self.assertEqual(len(decision["actions"]), 1)

    def test_the_only_code_difference_is_the_fork_constant(self):
        self.assertEqual(load("a").PUFF_COL, 7)
        self.assertEqual(load("b").PUFF_COL, 6)
        self.assertEqual(code_only("a"), code_only("b"))

    def test_b_differs_from_a_only_in_the_bait_column(self):
        run_a, _ = run(load("a").GargantuarScenario().decide)
        run_b, _ = run(load("b").GargantuarScenario().decide)
        # The fork is one parameter of one request, not an extra request: the two
        # branches issue the same number of requests with the same advance budgets,
        # and only the bait column differs. A second puff-shroom in the same tick is
        # rejected by the engine (card cooldown), which is why the column carries it.
        self.assertEqual(len(run_b), len(run_a))
        first = next(index for index, pair in enumerate(zip(run_a, run_b)) if pair[0] != pair[1])
        self.assertEqual(run_b[:first], run_a[:first])
        self.assertEqual(run_a[first], {"actions": [client_plant(8, 6, 7)], "advance_ticks": 0})
        self.assertEqual(run_b[first], {"actions": [client_plant(8, 6, 6)], "advance_ticks": 0})
        self.assertEqual(run_b[first + 1:], run_a[first + 1:])
        ticks_a, end_a = ticks_of(run_a)
        ticks_b, end_b = ticks_of(run_b)
        self.assertEqual((end_a, end_b), (BUDGET, BUDGET))
        self.assertEqual(ticks_a, ticks_b)

    def test_actions_match_the_client_helpers(self):
        run_a, _ = run(load("a").GargantuarScenario().decide)
        self.assertEqual(run_a[0], {"actions": [client_spawn(23, 6, 9)], "advance_ticks": 0})
        plants = [decision for decision in run_a
                  if any(action["op"] == "plant" for action in decision["actions"])]
        self.assertEqual(plants, [{"actions": [client_plant(8, 6, 7)], "advance_ticks": 0}])
        self.assertTrue(all(action["op"] in ("spawn", "plant")
                            for decision in run_a for action in decision["actions"]))

    def test_stage_constants_drive_the_timeline(self):
        module = load("a")
        module.GIANT_ROW, module.GIANT_COL = 2, 8
        module.PUFF_COL = 5
        module.APPROACH_TICKS, module.SMASH_TICKS = 10, 5
        module.ADVANCE_CHUNK_TICKS, module.TAIL_CHUNK_TICKS = 5, 3
        decisions, _ = run(module.GargantuarScenario().decide, budget=40)
        ticks, end = ticks_of(decisions)
        self.assertEqual(end, 40)
        self.assertEqual(decisions[0], {"actions": [client_spawn(23, 2, 8)], "advance_ticks": 0})
        planted = next(index for index, decision in enumerate(decisions)
                       if any(action["op"] == "plant" for action in decision["actions"]))
        self.assertEqual(ticks[planted], 10)
        self.assertEqual(decisions[planted], {"actions": [client_plant(8, 2, 5)], "advance_ticks": 0})
        # The smash window starts at the plant and the tail obeys its own chunk.
        self.assertEqual(ticks[planted + 1], 10)
        self.assertEqual([decision["advance_ticks"] for decision in decisions[planted + 1:]],
                         [5] + [3] * 8 + [1])
        self.assertTrue(all(decision["advance_ticks"] <= module.TAIL_CHUNK_TICKS
                            for decision in decisions[planted + 2:]))

    def test_short_budget_stops_inside_the_prefix(self):
        module = load("a")
        decisions, _ = run(module.GargantuarScenario().decide, budget=6)
        ticks, end = ticks_of(decisions)
        self.assertEqual(end, 6)
        self.assertEqual(decisions[-1]["advance_ticks"], 6)
        self.assertFalse(any(decision["actions"] for decision in decisions[1:]))

    def test_no_imports_and_no_zero_advance_stall(self):
        for branch in sorted(PATHS):
            with self.subTest(branch=branch):
                tree = ast.parse(PATHS[branch].read_text(encoding="utf-8"))
                imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
                self.assertEqual(imports, [], "the scenario must not read time, random or the game API")
                module = load(branch)
                first, worst = run(module.GargantuarScenario().decide)
                second, _ = run(module.GargantuarScenario().decide)
                self.assertEqual(first, second)
                # The runner aborts after 16 decisions without progress.
                self.assertLessEqual(worst, 3)


if __name__ == "__main__":
    unittest.main()
