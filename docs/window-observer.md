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
back-pressure a pipe or hold the child's GIL. Small atomic control files carry
PID binding and stop. The child takes a final sample after the stop request,
flushes/closes its raw file and writes a seal with sample count, byte count,
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

Windows control-file publication now has a separate path from immutable ready
and sealed evidence. The worker opens `control.json` with read/write/delete
sharing; the parent publishes a completed temporary file with `ReplaceFileW`
when the destination already exists. Its initial creation still uses
`os.replace`. Existing read handles retain the old complete document. The
ordinary `_write` used for ready/sealed evidence is unchanged. A publication
error is propagated, and any complete temporary file is retained; there is no
delete/recreate or partial-write fallback.

A simultaneous name lookup can still fail transiently during replacement.
Only native Windows errors 2, 5, 32 and 33 are retried, with at most eight
attempts and a 50ms retry budget, waiting at most 5ms between attempts. There
is no retry of malformed JSON or unrelated errors. A permanent missing or
inaccessible control file stops the worker and preserves a failed seal and
the original sample prefix. OS calls themselves cannot be forcibly bounded by
this retry budget; their real elapsed time is still subject to the existing
sample/probe checks.

The seal's additive `control_reads` diagnostics retain attempt count, transient
failure count, recovered-read count, maximum retry duration and the first32
error records (time, attempt, Win32/errno code and original message). Later
errors still contribute to the full count. Recovery does not create a sample,
reuse an old control document, change timestamps or reset the sampling clock.
The25ms interval and250ms maximum actual gap/probe duration remain unchanged.
Old seals without these diagnostics remain readable. This fix does not explain
or excuse the separate slow probes retained in run047.

`tests/test_window_control_io.py` exercises actual Windows held-reader
replacement, 400 publications delivered to each of three readers (all three
acknowledge before the next publication, without busy polling), recovery from a short exclusive lock,
permanent lock/missing-file failures, retained failed-publication bytes, and
a failed worker seal after a real raw prefix. An unbounded tight publisher
flood can exhaust the retry budget and correctly remains failure. These are
file-I/O fixtures, not game acceptance. The choice of replacement API follows
the documented distinction between
[moving and replacing files](https://learn.microsoft.com/en-us/windows/win32/fileio/moving-and-replacing-files)
and is tested against this Windows host rather than inferred from flags alone.

`tests/test_window_observer.py` includes an actual Windows observer while its
parent runs CPU work with a long Python thread-switch interval, parent/body
failure cleanup, and reader/startup/shutdown/corrupt-output fixtures. These tests
observe test processes, never a game; they are not live experiment acceptance.
A new long game replay with complete window evidence is still required.

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
