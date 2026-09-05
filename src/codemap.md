# src/

## Responsibility

Source root of the `obench` package (src-layout). Contains no direct code: everything lives in the single `obench` package, which hatchling packages as `src/obench` (see `[tool.hatch.build.targets.wheel]` in `pyproject.toml`).

## Integration

- Packaging: hatchling builds the wheel from this directory.
- Consumed by: the `bench` console script (`obench.cli:main`) and `tests/`.
- Sub-packages: [`src/obench/`](obench/codemap.md) (the harness), [`src/obench/web/`](obench/web/codemap.md) (Pages templates), [`src/obench/testing/`](obench/testing/codemap.md) (fake transport).
