# ocharness — working conventions

## Where the rules live
- `CONTEXT.md` is the glossary: terms of record + avoid-lists (pp, anchor, bracketed
  batch, slate, checker, S0/S1, prefix replay, measured hit-rate). Use those exact
  words in code, docs and tickets.
- `docs/methodology-v1.md` is the behavioral source of truth; harness tickets translate
  it into implementable work and its guardrails are never relaxed.
- The harness spec (issue #16) records the implementation/testing decisions: binding.

## Language
- Code, fixtures, datasets, commits, issue bodies: English. `README.md`/`README.es.md`
  are kept as pairs; the methodology stays Spanish.
- Code idiom: English docstrings carrying the protocol rationale; Spanish domain nouns
  (`manifiesto`, `modelo_api`, `ruta`, `veredictos`) — match the surrounding code.

## Hard guardrails (never relaxed)
- The API key is read only from the environment, never written to any dataset.
- No real run without its dry-run mark; raw JSONL is immutable; derivatives regenerate.
- The legacy account is frozen: never migrate it.

## Testing contract
- The CLI is the seam; assertions come only from produced artifacts and the requests
  the fake observed. Targeted unit tests are accepted for pure analysis functions
  (checkers, fixtures, the calibration analyzer) alongside that backbone.

## Work style
- One ticket → one feature commit; /code-review after each; findings fixed in that commit.
- Commits: `type(harness): imperative summary`, only on explicit request, no AI attribution.
- `--base` is the sandbox: nothing is written outside it. `task-a/` and `uv.lock` at the
  root are not ticket work.
