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

Both templates carry a byte-equal `<script id="shared-formulas">` block: the protocol's pure math (`isNum`, `isNull`, `newCostCore`, `verdictOf`, `medianOf`) — DOM-free, page-global-free. Python and the browser speak different languages, so the copy is hand-kept and enforced by `tests/test_template_js.py` (byte equality across pages, parity with `verdict_of`/`new_task_cost` on the hand-math goldens, and the shipped block re-verdicting the rendered dashboard's embedded cells over node — skipped when node is absent). The per-page wrappers (`newCostAt`, `newCostAt2`, `planTokens`, `effectiveS`) stay in each template's main script and feed the shared core; the two `money()` variants differ on purpose (presentation, not drift).

Loaders in the consumer: `_plantilla(name)` (analyze.py) reads each template from this package directory via `pathlib.Path(__file__).parent.joinpath("web", ...)`.

## Flow

Analysis JSON artifacts → template substitution → output HTML in `analysis/` (or the stamped `analysis-s<x>/` folder):

1. The CLI (`cmd_analyze` / release path) builds the doc with `analyze.build(...)`, then calls `analyze.write_bundle(base, doc, tabla=tabla)`.
2. `write_bundle()` (analyze.py) derives both rates payloads from `tabla` itself — the dashboard gets `_rates_map()` (analysis-cells models only), the calculator `_rates_map_full()` (an empty calculator is unrepresentable: the choice is not the caller's) — writes `analysis.json`, then:
   - `render_dashboard(doc, rates)` — fills the dashboard template's three placeholders.
   - `render_calculator(doc, rates)` — full per-model table map; every priced model gets a row and filter option.
3. Output: `analysis.json`, `dashboard.html`, `calculator.html` under the bundle folder.

## Integration

- Consumed by `src/obench/analyze.py` (template loaders + `render_dashboard`/`render_calculator`) and `src/obench/cli.py` (`write_bundle` call sites). `src/obench/predict.py` shares the "one template, two parameters" idiom but does not touch these files.
- Deployed by `.github/workflows/pages.yml`: on each release it re-derives the bundle with `bench analyze --release <tag>` (so the published pages match the released table vintage) and copies `releases/$TAG/analysis/dashboard.html` → `index.html`/`dashboard.html` and `analysis/calculator.html` → `calculator.html` onto the `gh-pages` branch.
