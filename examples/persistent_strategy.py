"""Minimal ordinary-Python usage; set --pid and --trace when invoking the file."""

import argparse

from llm_vs_zombies.client import connect
from llm_vs_zombies.session import SessionTrace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--trace", required=True)
    args = parser.parse_args()
    with SessionTrace(args.trace) as trace, connect(pid=args.pid, trace=trace) as game:
        observation = game.observe()
        initial_wave = observation["wave"]
        steps = []
        for _ in range(10):
            result = game.advance(1)
            steps.append(result["executed_ticks"])
            observation = result["observation"]
            if result["executed_ticks"] != 1 or observation["wave"] != initial_wave:
                break
        print({"executed_ticks": sum(steps), "version": observation["version"]})


if __name__ == "__main__":
    main()
