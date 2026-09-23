#pragma once
// Audit-only record of hosted AvZ cannon shots (issue #84, open item 2).
//
// `aCobManager.Fire` is not one of the runtime's actions. It reaches the engine
// through AAsm::Fire inside the hosted script's coroutine, so it never touches
// the request journal, the `action` audit kind or the replay tooling. This
// module is the whole audit policy for that path:
//
//   runtime/hosted_fire.cpp        the overlay hook's entry point
//   determinism/hosted_fire.cpp    the canonical record, the per-boundary state
//                                  component and the manifest declaration
//   determinism/audit.cpp          drains the records into audit/events.jsonl
//
// It is deliberately *not* a first-class `fire` action: no request id, no
// ordinal, no journal entry, no engine_replay step and no `action` audit kind.
// See docs/avz-script-hosting.md section 8.
//
// Compiled in both build modes, like logger/avz/hosted_observation.cpp: without
// LVZ_AVZ_HOSTED_FIRE_AUDIT every entry point is inert, no build calls them and
// nothing declares the mode. tests/avz_hosted_fire_tests.cpp is built both ways
// and asserts both behaviours.
#include <cstdint>
#include <nlohmann/json.hpp>

namespace lvz::determinism {
// Manifest value declaring the audited shape; the strict reader refuses a
// different one (src/llm_vs_zombies/audit_compare.py, hosted_fire_mode).
inline constexpr const char* kHostedFireMode = "hosted_fire_audit_v1";
// Audit event kind. Never "action": a hostile reader must be able to tell a
// hosted shot from a journaled request action.
inline constexpr const char* kHostedFireKind = "hosted_fire";

// True only in a build compiled with LVZ_AVZ_HOSTED_FIRE_AUDIT.
bool HostedFireEnabled();

// Queues one shot. `version` is the boundary version the runtime reported at
// the engine call; it becomes both the record's `version` and its `tick`, so a
// shot can never be stamped with an invented boundary. A missing version drops
// the record - an audit-only hook has no other way to fail closed.
void RecordHostedFire(int plantIndex, int plantId, int plantRow, int plantCol,
                      int targetRow, float targetCol, const nlohmann::json& version);

// One complete audit envelope per queued shot, in the order they happened,
// with `seq` starting at `firstSequence`. The writer owns the sequence counter.
nlohmann::json DrainHostedFireEvents(uint64_t firstSequence);

// Comparable per-boundary state: {"mode", "count", "digest"}. The digest is the
// audit's diagnostic FNV-1a over the payload's integer fields, little-endian
// 64-bit words, so the Python reader recomputes it from the records alone.
nlohmann::json HostedFireState();

// Manifest declaration of the mode (proves which semantics produced the records).
nlohmann::json HostedFireManifest();

// Drops queued records and counters (audit Initialize/Shutdown).
void ResetHostedFire();
} // namespace lvz::determinism
