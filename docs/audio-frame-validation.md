# Audio validation within one audit boundary

The strict reader checks the complete `sound_effects` subtree once per decoded boundary. It still checks every Foley history and slot, active parameter and resource identity, manager channel, health flag, App update count, allocation scope, and counter scalar. This changes neither recording formats nor the native engine.

`sound_counter.Evidence.frame_with_audio(audio, frame, raw)` composes the two checks in one synchronous call. It requires the actual `sound_effects.Evidence` checker for the same manifest, calls its full frame validation, and immediately checks the raw counter evidence against that result. Sequence, version, original-call identity, origin, arithmetic, monotonicity, range, and closure checks remain required.

The standalone `sound_counter.Evidence.frame(frame, raw)` still validates the entire current sound state. There is no public `validated_sound`, `skip_validation`, caller token, or cross-frame cache. The private shared counter implementation is reached only after validation in either entry point. The returned sound dictionary is not a reusable certificate: JSON Patch decoding can mutate it in place before the next frame, which must perform a new full validation.

Original audio mode and older allocation-none recordings without the counter-origin mode retain their checks. Complete initializer, early-seek suffix, raw-file binding, and closed-health validation continue through the existing readers. A failed check remains a failed audit; this optimization does not normalize or omit any state.

`tests/test_audio_frame_validation.py` checks valid sound with corrupted raw evidence, invalid sound with arithmetically valid raw evidence, sequential mutation of the same state object, independent helper calls, rejected forged checker arguments, and original/legacy modes. Existing audio, initialization, replay, terminal, seek, and archive tests provide the wider regression coverage.

This host-side change must be included in an explicitly frozen host source snapshot before new live use. Previously frozen hosts, native DLLs, sealed runs, and historical acceptance reports are unchanged.

Validation: all 287 Python tests passed. A before/after pair on the same sealed 1,000-tick real recording reduced complete sound validations from 4,010 to 2,010 during trajectory loading and from 4,004 to 2,004 during a complete stream pass. Loading took 16.0648 versus 14.0230 seconds; the stream pass with full evidence hashing took 18.5485 versus 15.7677 seconds. This single pair shows about 12.7% and 15.0% lower wall time, respectively; it is not a live-game throughput estimate.

All 2,000 boundaries, 40,764 particle calls, 71 controlled births, raw evidence and closing health stayed equal. The 627,248,646-byte canonical state stream retained SHA256 `15811b2ff2dbffd46d9931dc368f60a9232150be016239b0741071e4282aa08f`; the complete boundary evidence retained SHA256 `fc11005ac10ea237898067e2ee23fa42fd40bf1cc2b3d7cc31ecbb6edb307984`.
