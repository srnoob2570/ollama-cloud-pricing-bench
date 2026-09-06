# The ollama.com usage meter: what it exposes and at what granularity (2026-09-01)

Resolves issue
[srnoob2570/ollama-cloud-pricing-bench#2](https://github.com/srnoob2570/ollama-cloud-pricing-bench/issues/2).
Question: can an external client measure the legacy **GPU-time** plan's consumption per
request, and at what granularity? Everything marked as a probe was executed without
credentials on 2026-09-01 (UTC±, server `x-build-commit: fcafe397`); zero inference
requests, zero quota spent, no invented keys.

## 1. Does a meter endpoint exist, and what auth does it require?

- **`https://ollama.com/api/usage` exists.** Own probe: `GET /api/usage` without auth →
  **HTTP 401**, `content-type: application/json`, body `{"error":"invalid credentials"}`.
  The neighboring endpoints do not exist: `/api/usage/session`, `/api/usage/weekly`,
  `/api/user` and `/api/rates` return **404** JSON. That is, there is a single aggregated
  endpoint, not one per window.
- **CORS preflight blocked**: `OPTIONS /api/usage` → **405 Method Not Allowed** in JSON,
  with no `Access-Control-*` headers. The endpoint is not designed for third-party web
  clients; consuming it externally requires a script of your own (curl/Python) with
  credentials.
- **`GET https://ollama.com/settings` without a session → HTTP 303** to `/signin`: the
  meter page requires a web session.
- **Which credential it accepts is not verifiable without an account.** The 401 is
  identical with or without `Authorization: Bearer` (tested with the dummy value
  `oml-invalid-key-probe`: same body `{"error":"invalid credentials"}`), so the response
  alone cannot distinguish whether it accepts an API key, a session cookie, or both. The
  relevant part: the `ollama.com` API is documented with
  `Authorization: Bearer $OLLAMA_API_KEY`
  ([docs.ollama.com/api/authentication](https://docs.ollama.com/api/authentication.md),
  keys at `ollama.com/settings/keys`), and `{"error":"invalid credentials"}` is that API's
  error format. Inference vs. fact: that the `401` does not change with an invalid Bearer
  is consistent with "it accepts Bearer but this key is invalid". It does not confirm it.
- **Third-party evidence**: the only known external client that reads the meter,
  [dzackgarza/usage-limits](https://github.com/dzackgarza/usage-limits/blob/main/src/usage_limits/providers/ollama.py),
  does not use an API key: it scrapes the HTML of `https://ollama.com/settings` with
  the user's Chromium cookies (`browser_cookie3`) and `allow_redirects=False`.
  Confirmation that the externally proven path is the web session cookie, not a key.
- Unmet-demand context: [ollama/ollama#15663](https://github.com/ollama/ollama/issues/15663)
  asked for exactly this (quota via headers or the API body) and was closed as a
  duplicate on 2026-05-23 with no implementation and no maintainer response;
  [#17223](https://github.com/ollama/ollama/issues/17223) (dashboard, open) and
  [#17639](https://github.com/ollama/ollama/issues/17639) (open) keep asking for
  programmatic reads. No quota endpoint is documented.

## 2. What fields and units does it expose?

Primary source: the HTML of `ollama.com/settings`, decoded by the local userscript
`~/Documentos/srnoob2570/ollama-usage-breakdown/` ([ollama-usage-breakdown.user.js](https://github.com/srnoob2570/ollama-usage-breakdown/blob/main/ollama-usage-breakdown.user.js),
v1.3.7) and independently by dzackgarza's scraper. They match selector by selector.

| Data | Where it lives in the HTML | Actual unit |
|---|---|---|
| Total used of the period | `div[data-usage-track]` → `aria-label` `"Session usage N.N%"` / `"Weekly usage N.N%"` (dzackgarza) | **% of the window's quota** (1 decimal) |
| Per-model breakdown | `div[data-usage-segment]` per model: `data-model` (name), `data-requests` (integer), `aria-label` `"model: N requests"`, `style.width` (width as % of total used) | share ∝ width; **integer request count per model** |
| Windows | two tracks: **5 h session** and **7-day weekly** | fixed periods |
| Reset | `.local-time[data-time]` next to each meter (absolute ISO 8601) | timestamp |
| What is NOT there | — | **no GPU-seconds, no tokens, no dollars** in the meter |

- The userscript's README sums it up: *"Ollama only reports the overall 'X% used' and the
  request counts. Each model's share exists only in the page HTML, encoded as bar segment
  widths."* The per-model percentage is recomputed as `segment_width × total_%`
  (cited example: 84.2 % of a session at 10.7 % → 9.01 % of the quota).
- Key per the README: *"Percentages are read from Ollama's page (bar segment widths),
  **not from a private API**."* The userscript makes no calls (`@grant none`,
  zero network). The `data-usage-*` names are from Ollama's markup, which it watches with
  a `MutationObserver` because the page "re-renders in place" (htmx).
- User testimony about the live meter
  ([ollama/ollama#17639](https://github.com/ollama/ollama/issues/17639), 2026-08-09):
  "Session usage: 0%", "Weekly usage: ~68.5% remaining", "Models used this week includes
  `glm-5.2` with **thousands of requests**", plus "Extra usage balance: $0". Consistent
  with the table above (the weekly window also lists per-model request counts).
- The inference API contributes no quota: `docs.ollama.com/api/usage.md` documents the
  "usage" as per-request metrics (`prompt_eval_count`, `eval_count`, durations in
  ns), nothing account-level. And cloud responses carry no quota headers (central fact
  of [#15663](https://github.com/ollama/ollama/issues/15663): "Response headers also
  contain no quota metadata").
- The session/weekly meter is exclusive to the **legacy system**: the pricing page says
  of migrated plans that *"the session and weekly limits of the old plans no longer
  apply"* ([ollama.com/pricing](https://ollama.com/pricing)); the new plans are measured
  in $ credits and tokens. The study's frozen legacy Max plan still shows them.

## 3. Are the deltas attributable to individual requests or to aggregates?

**Aggregates with delay, with two partial aids for attribution** (not a per-request
counter):

1. **Resolution of the total: 1 decimal of % per track.** The `aria-label` gives the
   total with one decimal digit (10.7 % in the README example). The minimum observable
   increment between two reads is ~0.1 % of the period's quota. Over the 7-day window
   that resolution is coarse: it can correspond to many short requests or to none (even
   if the meter may refresh with more internal precision, what is published stays
   rounded).
2. **Segment widths: more internal resolution per model.** The width
   comes from `style.width` with decimals and the userscript rescales it to `% of quota`
   with 2 decimals; that is the best per-model magnitude proxy. But it is a fraction of
   the rounded total, not an independent absolute counter.
3. **Per-model request counts: exact cardinality.** `data-requests` / `aria-label`
   give the **integer number of requests per model** in the session (and the native
   weekly list shows them too, #17639). This makes it possible to know *how many*
   requests each model made between measurement N-1 and N. The magnitude is divided
   among them a posteriori.
4. **Backend lag: not verifiable from public sources.** That the page updates
   via htmx without reloading is a fact (README: *"Survives htmx updates"*); how long the
   backend takes to reflect a finished request in the served `%` is documented by no one.
   This is exactly what the live-verification child ticket must measure.

Conclusion: the strictly per-request delta (read → 1 request → read = that request's
exact %) is not guaranteed by any public data; what is observable is an
aggregate delta per model (total Δ% + per-model Δrequests), where attributing to each
individual request is a rounding of 0.1 % resolution (best case) and is subject to
an unquantified update lag.

## 4. Does frequent polling have limits?

- **Not documented.** Neither docs.ollama.com nor the FAQ on `ollama.com/pricing` publishes
  rate limits for `/api/usage` or `/settings` (the word "usage" on docs.ollama.com only
  exists as per-request metrics; on pricing, as credits/tokens). Distinguish from the
  **inference 429** for excessive use, which does exist and is the only thing documented
  by the community (e.g. the community PR "wait out Ollama 429 rate/usage limits").
- The unauthenticated probe cannot measure the real rate limit (testing it would require
  logging in and hitting the endpoint). Indirect signals: the infra is Google Frontend
  (`x-cloud-trace-context` headers), and `OPTIONS` is explicitly forbidden (405), which
  suggests a minimal surface. **Unverifiable**: the endpoint's 429/403 threshold.
- Indirect sign of third-party prudence: ollamatps.com samples inference "about hourly
  to avoid burning through the weekly free-tier balance". Nobody publishes measurements
  of polling the meter; quota consumption comes from inference requests, not
  (as far as we know) from reading the meter.

## Implications for the methodology

**What is measurable**: the observable unit of account of the legacy plan is the
**quota %** per window (5 h session and 7-day weekly), with a **per-model breakdown**
(share + request count) and an absolute **reset timestamp** (`data-time`). GPU-seconds
and tokens are not observable in any public source (confirms the glossary: the
GPU-time rate was never published).

**Which delta scheme is viable**:

1. **Strictly per-request: NOT reliably viable.** The meter publishes % with 1
   decimal and the backend has unknown lag; there is no guarantee that two reads around
   a single request isolate it. Any per-request estimate would depend on
   spacing the requests and reconciling windows, and would remain an estimate.
2. **Aggregate delta with lag: viable and recommended.** Polling the two windows
   records `(timestamp, total_%, per-model share, per-model n_requests, reset_at)`
   per track; the delta between polls attributes consumption at the **model + time-span**
   level, with exact request cardinality (Δrequests) and aggregate magnitude (Δ%).
   For the bench: run a workload, wait for the refresh, take a snapshot. The Δ%
   with Δrequests are the primary observation ("measured usage" in the glossary).
3. **The integer request count per model (`data-requests`) is the key connector** for
   fine attribution: given a span with 1 model and Δrequests = 1, that span's Δ%
   is the cost of that request (at 0.1 % resolution of the total, enough for
   T1 micro workloads? to be decided with data from the live-verification ticket).
4. **Technical path for the external client**: web session cookie (the one the community
   uses); API keys authenticate inference and there is no evidence that `/api/usage`
   accepts them. No CORS: polling from a script of your own (curl/Python with cookies),
   not from a page. Better still, inside the browser itself: the userscript already
   decodes the whole DOM and can export snapshots without touching the network.
5. **Pending on the live-verification ticket** (child of this issue): (a) the meter's
   real lag after a request; (b) the effective rate limit of polling; (c) confirmation
   of whether `Authorization: Bearer <key>` (or cookie only) opens `/api/usage`, and
   along the way its JSON, never observed by us, only inferred from the 401 and the
   htmx flow.

## Sources

- Own probe without credentials (2026-09-01): `401 {"error":"invalid credentials"}` on
  `/api/usage`; 404 on variants; 405 OPTIONS; 303 `/settings` → `/signin`.
- Local userscript `~/Documentos/srnoob2570/ollama-usage-breakdown/ollama-usage-breakdown.user.js`
  (v1.3.7, repo [srnoob2570/ollama-usage-breakdown](https://github.com/srnoob2570/ollama-usage-breakdown))
  + its [README](https://github.com/srnoob2570/ollama-usage-breakdown#what-it-does).
- [dzackgarza/usage-limits, providers/ollama.py](https://github.com/dzackgarza/usage-limits/blob/main/src/usage_limits/providers/ollama.py)
  (scrape with Chromium cookies; `aria-label` "Session/Weekly usage N%"; `div[data-time]`).
- [docs.ollama.com/api/usage.md](https://docs.ollama.com/api/usage.md) (usage = per-request
  metrics, ns/counts) · [docs.ollama.com/api/authentication.md](https://docs.ollama.com/api/authentication.md)
  (Bearer key for ollama.com) · [docs.ollama.com/llms.txt](https://docs.ollama.com/llms.txt)
  (index: there is no "usage meter" page nor cloud rate limits).
- [ollama/ollama#15663](https://github.com/ollama/ollama/issues/15663) (request for quota
  via the API; closed as duplicate) · [#17223](https://github.com/ollama/ollama/issues/17223)
  (dashboard; open) · [#17639](https://github.com/ollama/ollama/issues/17639)
  (meter read by hand; open).
- [ollama.com/pricing](https://ollama.com/pricing) ("session and weekly limits of the old
  plans no longer apply") · [ollamatps.com](https://ollamatps.com) (only indirect
  references to its own hourly sampling).
