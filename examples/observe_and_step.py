"""Run with python -m llm_vs_zombies.repl --pid PID --trace TRACE --script this_file.

The entrypoint injects game, plant and shovel. This example only advances time.
It does not select a level or claim deterministic replay.
"""

print("Capabilities:", game.hello_result)
initial = game.observe()
print("Initial:", initial["version"], "wave", initial["wave"], "sun", initial["sun"])

result = game.advance(1)
print("Requested:", result["requested_ticks"], "executed:", result["executed_ticks"])
print("Stop:", result["stop_reason"])
print("Now:", result["observation"]["version"])
