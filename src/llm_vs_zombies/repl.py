"""Persistent Python console with complete submitted-cell and client traces."""

from __future__ import annotations

import argparse
import code
import contextlib
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any, TextIO

from .client import Client, connect, plant, shovel
from .session import SessionTrace


class _TracedOutput:
    def __init__(self, target: TextIO, trace: SessionTrace, cell_id: str, stream: str):
        self.target, self.trace, self.cell_id, self.stream = target, trace, cell_id, stream

    def write(self, text: str) -> int:
        if text:
            self.trace.emit("cell_output", {"cell_id": self.cell_id, "stream": self.stream, "text": text})
        return self.target.write(text)

    def flush(self) -> None:
        self.target.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.target, name)


class RecordedConsole(code.InteractiveConsole):
    """Normal Python variables/functions persist between submitted cells."""

    def __init__(self, game: Client, trace: SessionTrace):
        super().__init__({"__name__": "__console__", "game": game, "plant": plant, "shovel": shovel},
                         filename="<lvz-repl>")
        self.trace = trace
        self._last_cell_ok = True

    def _execute(self, source: str, filename: str, symbol: str) -> bool:
        # Interactive partial input is not a completed cell yet.
        try:
            compiled = self.compile(source, filename, symbol)
        except (OverflowError, SyntaxError, ValueError):
            cell_id = uuid.uuid4().hex
            self.trace.emit("cell", {"cell_id": cell_id, "source": source, "filename": filename, "mode": symbol})
            self.trace.emit("cell_exception", {"cell_id": cell_id, "traceback": traceback.format_exc()})
            self.showsyntaxerror(filename)
            self.trace.emit("cell_complete", {"cell_id": cell_id, "ok": False})
            self._last_cell_ok = False
            return False
        if compiled is None:
            return True
        cell_id = uuid.uuid4().hex
        self.trace.emit("cell", {"cell_id": cell_id, "source": source, "filename": filename, "mode": symbol})
        success = False
        try:
            with contextlib.redirect_stdout(_TracedOutput(sys.stdout, self.trace, cell_id, "stdout")), \
                    contextlib.redirect_stderr(_TracedOutput(sys.stderr, self.trace, cell_id, "stderr")):
                try:
                    exec(compiled, self.locals)
                    success = True
                except SystemExit:
                    self.trace.emit("cell_exception", {"cell_id": cell_id, "traceback": traceback.format_exc()})
                    raise
                except BaseException:
                    self.trace.emit("cell_exception", {"cell_id": cell_id, "traceback": traceback.format_exc()})
                    self.showtraceback()
        finally:
            self._last_cell_ok = success
            self.trace.emit("cell_complete", {"cell_id": cell_id, "ok": success})
        return False

    def runsource(self, source: str, filename: str = "<lvz-repl>", symbol: str = "single") -> bool:
        return self._execute(source, filename, symbol)

    def execute_cell(self, source: str, filename: str = "<lvz-cell>") -> bool:
        """Execute a complete multi-statement cell. Return False on Python failure."""
        incomplete = self._execute(source, filename, "exec")
        if incomplete:
            # compile(..., 'exec') generally diagnoses incomplete source itself.
            raise ValueError("incomplete Python cell")
        return self._last_cell_ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audited persistent Python REPL for the local PvZ runtime")
    endpoint = parser.add_mutually_exclusive_group(required=True)
    endpoint.add_argument("--pid", type=int, help="PID of the game with the runtime DLL loaded")
    endpoint.add_argument("--endpoint", help=r"Local pipe, e.g. \\.\pipe\llm-vs-zombies-1234")
    parser.add_argument("--trace", type=Path, required=True, help="Append-only client session JSONL")
    parser.add_argument("--timeout", type=float, default=15.0, help="Whole request deadline in seconds")
    parser.add_argument("--script", type=Path, help="Execute a file with game/plant/shovel in scope, then exit")
    parser.add_argument("--seed", type=int, default=0, help="Seed used only for an unprepared ready controlled-draw game")
    parser.add_argument("--defer-preparation", action="store_true",
                        help="Leave one-time render preparation to an explicit initialization recipe")
    args = parser.parse_args(argv)
    try:
        with SessionTrace(args.trace) as trace:
            trace.emit("session_start", {"pid": args.pid, "endpoint": args.endpoint, "script": str(args.script) if args.script else None})
            try:
                with connect(pid=args.pid, endpoint=args.endpoint, trace=trace, timeout=args.timeout) as game:
                    if not args.defer_preparation:
                        from .initialization import ensure_render_prepared
                        ensure_render_prepared(game, args.seed)
                    console = RecordedConsole(game, trace)
                    if args.script:
                        return 0 if console.execute_cell(args.script.read_text(encoding="utf-8"), str(args.script)) else 1
                    console.interact(banner="PvZ REPL: game, plant, shovel are available. Calls use simulated ticks; inspect game.hello_result.",
                                     exitmsg="Client disconnected; session trace saved.")
                    return 0
            except Exception as error:
                trace.emit("session_exception", {"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()})
                raise
            finally:
                trace.emit("session_end", {})
    except Exception as error:
        print(f"lvz repl: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
