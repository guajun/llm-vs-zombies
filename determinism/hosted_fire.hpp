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
#include <cstddef>
#include <functional>
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

// Hands one complete audit envelope per queued shot, in the order they
// happened, to `sink`, with `seq` starting at `firstSequence`; returns how many
// records were drained. The sink is the audit writer's `Write(events, ...)`, so
// the offline test runs this very drain against a file and counts the lines it
// would have produced (the live 12-cannon run wrote the records but the digest
// they were reconciled against disagreed - see HostedFireState below).
size_t DrainHostedFireRecords(const std::function<void(const nlohmann::json&)>& sink,
                              uint64_t firstSequence);

// Comparable per-boundary state: {"mode", "count", "digest"}.
//
// The digest is the audit's diagnostic FNV-1a over the payload's integer fields
// in this fixed order - plant_index, plant_id, plant_row, plant_col,
// target_row, target_col_bits, tick - each encoded as one little-endian 64-bit
// word. The word comes from the value *the record stores*, not from the C++
// parameter that carried it: a uint32 field (plant_id) zero-extends and a
// negative JSON integer sign-extends, exactly like the Python reader's
// `value & 0xffffffffffffffff`. That rule is the whole contract between writer
// and reader; computing it from a differently typed copy of the same number
// (e.g. the signed `int` parameter while the record holds a uint32 plant id)
// silently breaks the reconciliation for every id >= 2^31.
nlohmann::json HostedFireState();

// Manifest declaration of the mode (proves which semantics produced the records).
nlohmann::json HostedFireManifest();

// Drops queued records and counters (audit Initialize/Shutdown).
void ResetHostedFire();
} // namespace lvz::determinism
