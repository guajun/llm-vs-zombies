"""Strict lifecycle_probes_closed footer and recorded receipt/capability shapes."""
import copy
import json
import unittest
from pathlib import Path

from llm_vs_zombies import audit_compare

VERSION = {"epoch": 1, "tick": 1, "revision": 0}
COUNTERS = {"captured": 2, "queued": 0, "delivered": 2, "overflow": 0, "wrong_thread": 0,
            "inactive_suppressed": 0, "classify_refused": 0, "read_failed": 0, "live_skips": 0,
            "unmatched_commits": 0, "pair_mismatch": 0, "overwritten_pending": 0, "faults": 0}
HEALTH = {"installed": False, "active": False, "healthy": True, "pending_candidate": False,
          "patched_sites": [], "reader_protected": False, "pending_callbacks": 0,
          "counters": dict(COUNTERS, persisted=2), "sites": []}
MANIFEST = {"lifecycle_probes": {"mode": "lvz.lifecycle-probes.v1", "enabled": True,
                                 "healthy": True, "installed": False, "pending_candidate": False,
                                 "probe_counters": dict(COUNTERS)}}


class Summary:
    def __init__(self, events):
        self.tail = events
        self.birth_count = 2


def event(kind, payload, version=None):
    return {"schema": "lvz.audit.v1", "seq": 0, "kind": kind, "version": dict(version or VERSION),
            "payload": payload}


def footer(*, probes=True, version=None, health=None, manifest=None, spawn=True):
    events = [event("recording_closed", {})]
    if spawn:
        events.append(event("spawn_hook_closed", {"healthy": True, "faults": 0, "overflow": 0,
                                                  "wrong_thread_calls": 0, "queued": 0,
                                                  "active_initializers": 0, "captured": 2}))
    if probes:
        events.append(event("lifecycle_probes_closed", health if health is not None else HEALTH,
                            version=version))
    return events


class ProbeFooterTests(unittest.TestCase):
    def check(self, events, **kwargs):
        options = {"spawn_required": True, "probes_required": True, "probes_allowed": True,
                   "manifest": MANIFEST}
        options.update(kwargs)
        audit_compare._closed_events(Summary(events), **options)

    def test_actual_writer_order_is_accepted(self):
        self.check(footer())

    def test_legacy_seven_footer_without_probes_still_passes(self):
        self.check(footer(probes=False), probes_required=False, probes_allowed=False)

    def test_missing_footer_is_rejected(self):
        with self.assertRaises(audit_compare.EvidenceError):
            self.check(footer(probes=False))
        with self.assertRaises(audit_compare.EvidenceError):
            self.check(footer(probes=False), probes_required=True, probes_allowed=True)

    def test_undeclared_footer_is_rejected(self):
        with self.assertRaises(audit_compare.EvidenceError):
            self.check(footer(), probes_allowed=False, probes_required=False)

    def test_early_reordered_and_duplicate_footers_are_rejected(self):
        events = footer()
        with self.assertRaises(audit_compare.EvidenceError):
            self.check([events[0], events[2], events[1]])
        with self.assertRaises(audit_compare.EvidenceError):
            self.check(events + [copy.deepcopy(events[-1])])
        with self.assertRaises(audit_compare.EvidenceError):
            self.check([events[2], events[0], events[1]])

    def test_version_mismatch_and_unhealthy_footer_are_rejected(self):
        with self.assertRaises(audit_compare.EvidenceError):
            self.check(footer(version={"epoch": 1, "tick": 2, "revision": 0}))
        bad = copy.deepcopy(HEALTH)
        bad["healthy"] = False
        with self.assertRaises(audit_compare.EvidenceError):
            self.check(footer(health=bad))
        bad = copy.deepcopy(HEALTH)
        bad["counters"]["faults"] = 1
        with self.assertRaises(audit_compare.EvidenceError):
            self.check(footer(health=bad))
        bad = copy.deepcopy(HEALTH)
        bad["installed"] = True
        with self.assertRaises(audit_compare.EvidenceError):
            self.check(footer(health=bad))
