# AGENTS.md

`geni`, `committe` and `itera` wrap a tool (`dic`/`llm`) that has flags of
its own.  A wrapper must never swallow a flag meant for what it wraps.

- Parse the wrapper's flags left to right.
- The first `--` ends them; the rest goes verbatim to the wrapped command.
- An unknown flag passes through, not rejected, so a flag added to `dic`
  works here unedited.
- `committe -- -f` names the wrapped `-f` when both define one.
