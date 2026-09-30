# Contributing

Agent Workspace coordinates task state. Agents use external tools to perform
work; the board does not run code or manage resources. Keep changes within that
scope unless the user explicitly approves a new direction.

Before committing, run:

```sh
uv sync --locked --extra test --extra mcp
uv run --locked python -m pytest -q
uv build --no-sources
```

Add a focused test when changing a task transition, persistence or the transport
boundary. Do not replace meaningful checks with tests that only mirror code.
Keep task recovery explicit: no expiry, runtime limits, automatic retries,
resource shutdown or watchdogs. Cancellation only changes board state.

Update the relevant documentation when behavior changes. CI checks tests and
package build; there is no mandatory documentation or lint gate. Keep removed
implementations in Git history rather than copying them back into the active tree.
