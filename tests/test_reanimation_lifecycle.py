"""Issue 20: declared DropFlag lifecycle, independently bound to raw fields."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from llm_vs_zombies.audit_compare import AuditLog, EvidenceError, digests
from test_audit_compare import animation_audit

RULES = ["owner_dead", "plant_squished_remove_effects", "zombie_flag_dropped"]
OWNER_ID = 3832741926
FLAG_ID = 3893952519


def fixture(directory, *, flag=FLAG_ID, has_object=0, zombie_type=1, status="expired", declared=True):
    rows = animation_audit(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["coverage"]["reanimations"]["owner_retirement_rules"] = RULES if declared else RULES[:2]
    (directory / "manifest.json").write_text(json.dumps(manifest))
    state = rows["state-deltas.jsonl"][0]["initial"]
    state["board"].pop("animation")
    anchor = "zombies/e4730026"
    body_ref = {"status": "live", "node": anchor + "/body#0"}
    flag_ref = {"status": status}
    if status in ("live", "retiring"):
        flag_ref["node"] = anchor + "/special_head#0"
    state["zombies"] = {"slots": {"38": {"id_or_free_next": OWNER_ID, "fields": {
        "00000024": zombie_type, "000000ba": 0, "000000bb": 0, "000000bc": has_object,
        "000000bd": 1, "000000c8": 70, "000000ec": 0, "00000118": body_ref, "00000144": flag_ref}}}}
    state["reanimations"]["nodes"] = {body_ref["node"]: {"state": {"type": 0, "dead": False}, "owners": [anchor + "/body"]}}
    raw = rows["reanimation-handles.jsonl"][0]["initial"]
    raw["pool"].update(capacity=1024, used=203, free_head=35, next_key=59560)
    body_link = raw["links"][0]
    body_link.update(path="/zombies/slots/38/fields/00000118", anchor=anchor+"/body",
                     logical_node=body_ref["node"], normalized_reference=body_ref)
    head_link = {"path": "/zombies/slots/38/fields/00000144", "anchor": anchor+"/special_head",
                 "raw_handle": flag, "slot": flag & 0xffff, "owner_dead": False,
                 "lookup_matches": status in ("live", "retiring"), "actual_slot_id": None,
                 "normalized_reference": flag_ref}
    if declared:
        head_link.update(owner_zombie_type=zombie_type, owner_has_object=bool(has_object))
    if status in ("live", "retiring"):
        raw["actual_slot_ids"][str(flag & 0xffff)] = flag
        head_link.update(actual_slot_id=flag, logical_node=flag_ref["node"])
        state["reanimations"]["nodes"][flag_ref["node"]] = {
            "state": {"type": 142, "dead": status == "retiring"}, "owners": [anchor+"/special_head"]}
    elif flag:
        head_link.update(lookup_failure="not_allocated", retirement_reason="zombie_flag_dropped")
    raw["pool"]["count"] = len(raw["actual_slot_ids"])
    raw["links"].append(head_link)
    save(directory, rows)
    return rows


def save(directory, rows):
    state = rows["state-deltas.jsonl"][0]["initial"]
    for index in range(2):
        actual = copy.deepcopy(state)
        actual["board"]["tick"] = index
        rows["checksums.jsonl"][index]["digests"] = digests(actual)
    for name, values in rows.items():
        (directory / name).write_text("".join(json.dumps(row) + "\n" for row in values))


class FlagRetirementTests(unittest.TestCase):
    def read(self, **kwargs):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name) / "audit"
        rows = fixture(directory, **kwargs)
        return directory, rows

    def test_public_readiness_rejects_any_declared_test_fixture(self):
        from types import SimpleNamespace
        from llm_vs_zombies.evaluation import _require_production_runtime
        _require_production_runtime(SimpleNamespace(hello_result={"game": {}}))
        for descriptor in ({"mode":"test_flag_drop_v1"}, False, None):
            with self.assertRaisesRegex(ValueError,"test_fixture runtime"):
                _require_production_runtime(SimpleNamespace(hello_result={"game":{"test_fixture":descriptor}}))

    def test_real_owner_expiry_retains_live_body_and_same_semantics_across_raw_generations(self):
        left, _ = self.read()
        right, _ = self.read(flag=FLAG_ID+65536)
        a = AuditLog(left, require_closed=True); b = AuditLog(right, require_closed=True)
        self.assertEqual([frame.state for frame in a.frames], [frame.state for frame in b.frames])
        state = a.frames[0].state
        self.assertEqual(state["zombies"]["slots"]["38"]["fields"]["00000144"], {"status": "expired"})
        self.assertEqual(len(state["reanimations"]["nodes"]), 1)
        self.assertNotEqual((left/"reanimation-handles.jsonl").read_bytes(), (right/"reanimation-handles.jsonl").read_bytes())

    def test_matching_flag_still_keeps_live_or_retiring_node_and_null_is_distinct(self):
        for status in ("live", "retiring", "null"):
            with self.subTest(status=status):
                directory, _ = self.read(status=status, **({"flag":0} if status=="null" else {}))
                audit=AuditLog(directory,require_closed=True)
                self.assertEqual(len(audit.frames[0].state["reanimations"]["nodes"]),1 if status=="null" else 2)

    def test_other_type_or_still_carried_and_old_manifest_do_not_gain_exception(self):
        for args in ({"zombie_type":0},{"has_object":1},{"declared":False},{"flag":7},{"flag":0xe8190400}):
            with self.subTest(args=args):
                directory, _ = self.read(**args)
                with self.assertRaises(EvidenceError):AuditLog(directory,require_closed=True)

    def test_forged_or_missing_flags_and_wrong_raw_lookup_are_rejected(self):
        mutations = (
            lambda state, raw: raw["links"][1].update(owner_has_object=True),
            lambda state, raw: raw["links"][1].update(owner_zombie_type=2),
            lambda state, raw: raw["links"][1].pop("owner_has_object"),
            lambda state, raw: state["zombies"]["slots"]["38"]["fields"].pop("000000bc"),
            lambda state, raw: state["zombies"]["slots"]["38"]["fields"].update({"000000bc":2}),
            lambda state, raw: raw["links"][1].update(actual_slot_id=FLAG_ID),
            lambda state, raw: raw["links"][1].update(retirement_reason="owner_dead"),
            lambda state, raw: raw["pool"].update(used=3),
            lambda state, raw: raw["links"][0].update(owner_has_object=False,owner_zombie_type=1),
        )
        for mutation in mutations:
            directory, rows = self.read()
            mutation(rows["state-deltas.jsonl"][0]["initial"],rows["reanimation-handles.jsonl"][0]["initial"])
            save(directory, rows)
            with self.assertRaises(EvidenceError):AuditLog(directory,require_closed=True)

    def test_body_or_other_role_dangling_still_fails(self):
        for key in ("00000118", "00000140", "00000150"):
            directory, rows = self.read()
            state=rows["state-deltas.jsonl"][0]["initial"]
            raw=rows["reanimation-handles.jsonl"][0]["initial"]
            link=raw["links"][1]
            link.update(path="/zombies/slots/38/fields/"+key)
            state["zombies"]["slots"]["38"]["fields"][key]=state["zombies"]["slots"]["38"]["fields"].pop("00000144")
            save(directory,rows)
            with self.assertRaises(EvidenceError):AuditLog(directory,require_closed=True)

    def test_slot_reuse_keeps_old_generation_evidence(self):
        directory,rows=self.read()
        raw=rows["reanimation-handles.jsonl"][0]["initial"]
        raw["actual_slot_ids"]["7"]=FLAG_ID+65536;raw["pool"]["count"]+=1
        raw["links"][1].update(actual_slot_id=FLAG_ID+65536,lookup_failure="generation_mismatch")
        save(directory,rows)
        self.assertEqual(len(AuditLog(directory,require_closed=True).frames),2)


if __name__ == "__main__":unittest.main()
