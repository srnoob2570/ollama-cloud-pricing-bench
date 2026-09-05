# src/obench/web/

## Responsibility

Static HTML report templates for the two analysis artifacts published via GitHub Pages:

- `dashboard_template.html` — the cost-analysis page (`dashboard.html`): verdict hero, per-cell scoreboard, cache-sensitivity controls. All interactivity (model filter, three-state theme, cache control, live recomputation) is client-side JS inside the template.
- `calculator_template.html` — the plan token-budget page (`calculator.html`): per-plan pricing matrices for the Pro/Max credits at preset and custom in : out token splits.

The templates carry the full markup, CSS and JS; no HTML lives in Python strings. Data rides inline as `<script type="application/json">` blocks, so each rendered page is a single self-contained file with no sibling fetches. Styling is CDN-first (Tailwind browser CDN, Google Fonts, Lucide) because GitHub Pages serves online.

## Design

Plain static HTML with `__TOKEN__` placeholders, filled by pure `str.replace()` — no templating engine:

- `__OPCIONES__` — `<option>` list for the model filter `<select>` (built with `html.escape`).
- `__DATOS__` — the analysis doc as inline JSON in `#analysis-data`.
- `__RATES__` — the per-model rates map as inline JSON in `#rates-data`.

JSON payloads are serialized with `ensure_ascii=False` and `"</"` is escaped to `"<\\/"` so the payload cannot close its own script tag. Every dynamic value elsewhere escapes through `html.escape` (Python side) or the JS `esc()` helper (client side); rendering uses `textContent`.

Loaders in the consumer: `_plantilla_dashboard()` and `_plantilla_calculator()` (analyze.py:1164, analyze.py:1175) read each template from this package directory via `pathlib.Path(__file__).parent.joinpath("web", ...)`.

## Flow

Analysis JSON artifacts → template substitution → output HTML in `analysis/` (or the stamped `analysis-s<x>/` folder):

1. The CLI (`cmd_analyze` / release path in cli.py:952, cli.py:1011) builds the doc with `analyze.build(...)`, then calls `analyze.write_bundle(base, doc, rates=..., calculator_rates=...)`.
2. `write_bundle()` (analyze.py:1120) refuses to write without `calculator_rates` from `rates_map_full(tabla)` (an empty calculator prices zero models), writes `analysis.json`, then:
   - `render_dashboard(doc, rates)` (analyze.py:1186) — `rates` from `rates_map()` (analysis-cells models only); fills the dashboard template's three placeholders.
   - `render_calculator(doc, calculator_rates)` (analyze.py:1214) — full per-model table map from `rates_map_full()`; every priced model gets a row and filter option.
3. Output: `analysis.json`, `dashboard.html`, `calculator.html` under the bundle folder.

## Integration

- Consumed by `src/obench/analyze.py` (template loaders + `render_dashboard`/`render_calculator`) and `src/obench/cli.py` (`write_bundle` call sites). `src/obench/predict.py` shares the "one template, two parameters" idiom but does not touch these files.
- Deployed by `.github/workflows/pages.yml`: on each release it re-derives the bundle with `bench analyze --release <tag>` (so the published pages match the released table vintage) and copies `releases/$TAG/analysis/dashboard.html` → `index.html`/`dashboard.html` and `analysis/calculator.html` → `calculator.html` onto the `gh-pages` branch.
