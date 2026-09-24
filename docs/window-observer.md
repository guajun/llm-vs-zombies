# Independent window-observation process

Issue #16 preserves the failed 041 long cold-replay acceptance: the simulation
completed 5,000 ticks without an online mismatch, but its observer had a 0.691s
gap and therefore insufficient window evidence. Several other gaps exceeded
0.25s. CPU/GIL contention in the parent is a plausible contributor, not a
measured causal conclusion about those old samples. Old reports are unchanged.

`LaunchWindowMonitor` now starts a separate Python process with
`CREATE_NO_WINDOW`. It executes only `window_observer.py`, not the caller's
driver or the game. It reads foreground HWND/owner and window visibility; it
does not activate a window, change visibility, send input, or draw. The target
interval remains 25ms and the maximum accepted gap remains 250ms.

The worker must finish a valid first sample and acknowledge its actual process
creation identity before the context enters. Startup callers bind the real
launcher PID before exiting the context:

```python
with LaunchWindowMonitor(evidence_directory=run / 'decisions/window-observer-launch') as monitor:
    launcher = start(...)
    monitor.bind_pid(launcher['pid'])
result = monitor.result(launcher['pid'])
```

For an already launched game, pass `pid=launcher['pid']` to the constructor and
keep the context around the actual work. The target is bound by PID plus real
creation time using a query-only handle. Binding cannot change process identity.
Subclasses overriding the old thread's `sample()` method now fail explicitly:
replace them with the `pid=` argument, rather than silently losing per-sample
owned-window visibility. Collection before context exit is also rejected.

Raw samples are flushed as JSONL directly by the child; parent parsing cannot
back-pressure a pipe or hold the child's GIL. A write-once control document and
one write-once PID binding carry the setup, and a named event carries the stop
request (see the control-channel section below). The child takes a final
scan-backed sample after the stop request, flushes/closes its raw file and
writes a seal with sample count, byte count,
SHA256, source SHA256, process identities, times, errors and actual exit reason.
The parent checks that the sample interval covers its entire context, then
validates the raw stream and seal. It does **not** append a parent sample after
potentially expensive parsing. Runtime results use `lvz.launch-windows.v3` and
retain `observer` provenance; the verdict fields remain compatible with the
evaluation gate.

The evidence directory must be empty and is claimed with an exclusive writer
lock. A conflicting directory is never changed. Defaults use a retained temp
directory; reproducible experiments should specify a directory under the run's
`decisions/` so the normal archive includes every raw file. The public evaluation
launcher does this. Host-side helper changes used with a frozen native run must
be independently saved/hash-bound, not substituted into an old recording.

Ready waits at most five seconds; ordinary shutdown waits two seconds. A stuck
worker can only be terminated via its own `Popen` handle, followed by another
bounded wait. If still alive, its lock remains and finalization cannot pass. If
dead without a valid seal, the parent records an incomplete shutdown and retains
the raw prefix; it never manufactures healthy evidence. A held parent handle
lets the worker stop on parent death. A target exit also ends observation with
an incomplete reason. Observation has hard limits of 24 hours, four million
samples and 2GiB raw data; reaching a limit cannot pass.

Missing output, bad ordering/ownership/hash, unresolved foreground ownership,
probe errors, excessive probe duration, excessive sample gaps, or incomplete
lifecycle coverage cannot pass. Any sampled game foreground or visible owned
window is failure, including a visible intermediate sample followed by a hidden
final sample. Samples cannot be reordered to hide bad chronology. System-wide
scheduling or disk stalls can still exceed 250ms and remain unverified. Finite
sampling remains unable to exclude activation between reads.

## Control channel (issue #21)

`control.json` is no longer a mutable document. The parent claims the evidence
directory, creates one session-local named event, publishes `control.json`
exactly once (schema, token, parent identity, sampling interval, scan interval
and the stop event's name) and publishes `target.json` at most once when the
game PID is bound. Both publications are a single rename onto a name that does
not exist yet, so the worker can only observe a document absent or complete, and
`_publish_once` refuses a second publication of the same name instead of
rewriting it. No reader has to survive a replacement, and nothing the parent
writes is ever rewritten; `samples.jsonl` stays append-only and single-writer as
before.

The only mutable cross-process channel is that named event object
(`Local\lvz-window-observer-stop-<token>`), created by the parent before the
worker starts and opened by the worker once. `__exit__` sends the stop request
with a single `SetEvent`; the worker polls it with a zero-timeout
`WaitForSingleObject`, writes one final scan-backed sample and seals. Parent
death is still detected through the held parent process handle, so a worker
whose parent died still ends with an incomplete `parent_exited` seal instead of
waiting forever.

Publishing a name can transiently deny a concurrent open for about a millisecond
while the directory entry becomes visible. The worker reads the write-once
documents through `_read_published_document`, which retries only those transient
errors (Python reports `PermissionError(errno=13)`; the raw Win32 codes are
5/32/33) with at most eight attempts and a 0.5s budget, records `control_reads`
and `target_reads` (transient count, recovered reads, maximum retry duration and
the first 32 error records) in the seal, and then fails naming the component.
Because the document never changes, a retry can never pick up a different or
partial document - unlike the replaced `control.json` of the previous design.
Missing documents, invalid JSON, an identity mismatch and a missing stop channel
fail immediately and name their component (`control document`, `stop channel`,
`target binding document`, `target identity`). The 25ms interval and the 250ms
maximum actual gap/probe duration are unchanged, and a permanent channel failure
preserves a failed seal plus the original sample prefix.

| Component | Bound | Receipt on failure |
|---|---|---|
| startup ready | 5s, or the owned child exits | `observer startup: ...`; the context is never entered |
| stop event | `SetEvent`, then a 2s wait; a hung child is terminated by its own `Popen` handle and waited at most 2s again | `observer stop channel: ...`, `observer did not stop within deadline; owned observer terminated`, `observer exit status N` |
| control/target documents | 8 transient attempts or 0.5s per document | seal `failure.component` plus `control_reads`/`target_reads` |
| lifetime | 24h, four million samples, 2GiB | `observer_limit` seal, cannot pass |

A worker killed without a seal leaves `parent-closed.json` and no `sealed.json`;
the parent records the exit status and the gate stays `unverified`. Nothing
waits indefinitely, and no failure is manufactured into a sample.

## Window identity (issue #21 follow-up)

Process identity was already bound to a query-only handle plus the real creation
time; window identity was not. The worker enumerated every top-level window on
the system on every 25ms tick and filtered by PID, which is both the dominant
per-sample cost and a source of multi-hundred-millisecond probe stalls when two
worlds share one desktop.

`WindowTracker` now discovers the target's top-level HWNDs in a scan and then
re-verifies exactly those handles every sample with `IsWindow`,
`GetWindowThreadProcessId` (the owner must still be the target PID) and
`IsWindowVisible`. The full enumeration repeats only while nothing is held (the
launch phase, before resume creates the window), on request, every
`scan_interval_seconds` (1.0s) as a backstop for windows created after
discovery, and for the sample taken after a stop request so the final visibility
claim is scan-backed. Losing the last held window forces an immediate
rediscovery inside the same sample. A destroyed handle - or one whose value was
reused by another process - is dropped and counted in
`window_discovery.dropped_destroyed`/`dropped_reused`, so a reused HWND value is
never reported as the game's window and another process's windows never enter
this evidence. The seal retains the discovery diagnostics (`scans`,
`verifications`, `discovered`, `dropped_*`, `maximum_scan_seconds`,
`scan_interval_seconds`) and the v3 evidence repeats them under
`observer.window_discovery`. A reused handle inside the same target process
cannot be told apart from the original by any Win32 query; both are that
process's windows, so the visibility and foreground conclusions do not change.
No injection, activation, visibility change or desktop input is added: every
call stays a read-only query.

## Private-launch claim

`private_launch` is judged from this observation's own evidence: the sampled
foreground handle must not be one of the held windows, the sampled foreground
owner must not be the target PID, and no owned window may be visible in any
sample - an intermediate visible sample is never erased by a hidden final one.
`foreground_unchanged` stays in the evidence together with
`foreground_change_cause: "not_inferred"`,
`foreground_change_used_as_evidence: false` and a `claims` block that records
the basis: equality or inequality of two global foreground handles is never a
reason for a pass or a fail, because another process can create or activate a
window while this run does nothing.

## Tests

`tests/test_window_control_io.py` covers the write-once publications under a
reader hammering the publication instant (every successful read is the complete
document, and every transient denial recovers inside the bound), the refusal of
a second publication and of overwriting a retained temporary, real
cross-process delivery of the named stop event, refusal to adopt an existing
channel name, and worker failures that name `control document`, `stop channel`,
`target binding document` and `target identity` while keeping the raw prefix.
`tests/test_window_tracking.py` covers held-handle verification through an
injectable read-only query surface (discovery once, per-sample verification,
periodic backstop discovery, destroyed and reused handles, replacement inside
the same sample, another process's window churn), the private-launch claims, the
unchanged 25ms/250ms limits, and one real Windows fixture in which an invisible
top-level window created by a child process is discovered by the real scan and
observed to `pass` end to end. Those fixtures never start or interact with a
game. `tests/test_window_observer.py` includes an actual Windows observer while
its parent runs CPU work with a long Python thread-switch interval, write-once
publication checkpoints, a killed worker, parent/body failure cleanup, and
reader/startup/shutdown/corrupt-output fixtures. None of this is live game
acceptance: a new long game replay with complete window evidence is still
required.

Issue #94 (one long-run probe above 250ms cancelling replay eligibility, and
per-tick coverage of granted tick ranges) is deliberately untouched: a single
slow probe still fails exactly as before, and the 047 (380.44/385.10ms),
`jd12-flags2-01` (0.605s) and `m1-par-c2` (0.589s) archives keep their recorded
verdicts. The raw stream keeps `seq`, `monotonic_seconds`,
`probe_finished_seconds` and `target`, which is the pairing cursor that later
work needs.

Run `jd12-flags2-01-s42-c0` (经典十二炮, 16,000 ticks, 1,642s runtime window)
is the longest runtime observation so far and shows the limitation above from
the other side. 65,437 samples saw the game hidden with no sampled game
foreground or visible owned window, but exactly one probe took 0.605s: 24x the
25ms target, above the 250ms ceiling and 5x the next largest probe (0.118s).
The runtime evidence was therefore correctly `unverified`, and the suite
skipped the cold replay it gates. Nothing was wrong with the recording: all
recording, cleanup, archive and host gates for that source passed, yet the
skip reason read "source infrastructure or recording did not pass" and sent
the reader to the replay side. Retention receipts now carry
`infrastructure_components` and `infrastructure_failures`, and the case names
the blocking component (`runtime_windows` here) in `replay_blockers` and
`replays_skipped`. The 25ms interval, the 250ms maximum gap and the refusal to
pass incomplete observation are unchanged; a long run still needs, rather than
deserves, its own clean window evidence. `m1-par-c2-s42-c1` shows the same
single-outlier shape under parallel cold load (0.589s in a 273s window).
