# Obench — working conventions

## Where the rules live
- `CONTEXT.md` is the glossary: terms of record + avoid-lists (pp, anchor, bracketed
  batch, slate, checker, S0/S1, prefix replay, measured hit-rate). Use those exact
  words in code, docs and tickets.
- `docs/methodology-v1.md` is the behavioral source of truth; harness tickets translate
  it into implementable work and its guardrails are never relaxed.
- The harness spec (issue #16) records the implementation/testing decisions: binding.
- `codemap.md` at the root is the repository atlas; each mapped folder has its own
  `codemap.md`.

## Developer commands (uv)
- Setup: `uv sync` → verify with `uv run bench --help`. Python >= 3.12, hatchling.
- Tests (offline, full suite in seconds): `uv run pytest -q`.
  Single file: `uv run pytest tests/test_release.py -q`.
  Single test: `uv run pytest tests/test_run.py -q -k resume`.
- `ruff` is NOT a project dep — it lives in the pre-commit env. Run
  `pre-commit run --all-files` (one-time install: `pre-commit install`), or per-check:
  `uvx ruff@0.16.5 check --select=E9,F821 src tests` (syntax + undefined names only;
  do not run the default ruff ruleset) and
  `uvx ruff@0.16.5 format --check src tests` (line-length 100).
- Order when finishing work: format → ruff check → pytest.

## bench CLI basics
- `--base <dir>` goes BEFORE the subcommand: `bench --base <dir> analyze ...`.
- `--json` exists on every subcommand: parseable stdout, progress on stderr.
- Exit codes: `0` ok · `1` aborted run (preflight/runner) · `2` usage/validation/gate ·
  `3` unimplemented subcommand.
- Free & offline: tests, `dry-run`, `analyze`, `predict`, `status`, `release` (gh only).
- Quota-spending — only with explicit owner instruction in that same conversation:
  `run`, `resume`, `probe-concurrency`, `calibrate-cache`.
- Gate: `dry-run --level <L> --reps <N>` writes the mark; one mark enables exactly one
  run with the same level/table/reps. `resume` requires a fresh mark too.
- Price change = new `pricing/<version>.json` + re-derive with `--table-version`;
  never edit a shipped table or past raw data.
- `analyze` refuses cleanly without local raw data; published datasets are consumed
  offline via `bench analyze --release <tag>` (the release's own table vintage).
- `OLLAMA_API_KEY` is read only from the environment; nothing auto-loads `.env`.

## Hard guardrails (never relaxed)
- The API key is read only from the environment, never written to any dataset.
- No real run without its dry-run mark; raw JSONL is immutable; derivatives regenerate.
- The legacy account is frozen: never migrate it.

## Language
- Code, fixtures, datasets, commits, issue bodies: English. `README.md`/`README.es.md`
  are kept as pairs; the methodology stays Spanish.
- Code idiom: English docstrings carrying the protocol rationale; Spanish domain nouns
  (`manifiesto`, `modelo_api`, `ruta`, `veredictos`) — match the surrounding code.

## Testing contract
- The CLI is the seam; assertions come only from produced artifacts and the requests
  the fake observed (`src/obench/testing/fake.py`, wired by `tests/conftest.py`).
  Targeted unit tests are accepted for pure analysis functions (checkers, fixtures,
  the calibration analyzer) alongside that backbone.
- Test tables validate against `pricing/2026-08-31.json` via conftest's
  `standard_table()`; moving that snapshot breaks the T2/T3 preflight tests.

## Work style
- One ticket → one feature commit; /code-review after each; findings fixed in that commit.
- Commits: `type(harness): imperative summary`, only on explicit request, no AI attribution.
- `--base` is the sandbox: nothing is written outside it. Stage with explicit paths;
  `uv.lock` at the root is not ticket work.
- `LOCAL.md` is a local-only runbook excluded via `.git/info/exclude` — never commit it.

## Repository Map

A full codemap is available at `codemap.md` in the project root.

Before working on any task, read `codemap.md` to understand:
- Project architecture and entry points
- Directory responsibilities and design patterns
- Data flow and integration points between modules

For deep work on a specific folder, also read that folder's `codemap.md`.
