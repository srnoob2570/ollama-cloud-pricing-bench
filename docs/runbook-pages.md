# Runbook: publishing the dashboard to GitHub Pages

Operational runbook for the release → analysis → gh-pages publication flow.
It mirrors `.github/workflows/pages.yml` (release published / workflow_dispatch)
and documents the manual, fully-offline equivalent used when the `gh` CLI is
unavailable. All analysis steps run with zero API quota: a release dataset is
priced by its own table snapshot and never re-measures.

## Prerequisites

- `uv` and SSH git access to `origin` (the `gh` CLI token may be invalid; git
  over SSH is enough for every step below).
- The release's dataset copy under `releases/<tag>/` (the local mirror of the
  published GitHub release; `analyze --release` reads only this).

## Steps

1. **Land the code first.** The deployed page is always the released dataset
   rendered by the code at the publish ref — commits on `main` are what the
   page will show. Keep the suite green before continuing: `uv run pytest -q`.

2. **Pick the release tag** to publish (the latest one, normally):

   ```
   TAG=run-T2-20260902T051123Z-ebf685c7
   ```

3. **Read the release's own table version** (each release is priced by the
   table snapshot it shipped with — never a newer one):

   ```
   jq -r .table_version releases/$TAG/metadata-*.json
   ```

4. **Re-derive the dashboard from the release** (offline, writes
   `releases/$TAG/analysis/dashboard.html`):

   ```
   uv run bench analyze --release "$TAG" --repo srnoob2570/ollama-cloud-pricing-bench \
     --table-version "$(jq -r .table_version releases/$TAG/metadata-*.json)"
   ```

5. **Sanity-check the page** before publishing (ids, sections, no stale
   copy): open `releases/$TAG/analysis/dashboard.html` or grep for the
   sections you expect.

6. **Push `main`** (the code the page is rendered by):

   ```
   git push origin main
   ```

7. **Publish gh-pages** (the page lives at `index.html` on that branch):

   ```
   git fetch origin gh-pages
   git switch gh-pages
   cp "releases/$TAG/analysis/dashboard.html" index.html
   git add index.html
   git commit -m "pages: dashboard re-derived from $TAG"
   git push origin gh-pages
   git switch main
   ```

   Untracked working-tree dirs (`analysis/`, `batches/`, `releases/`,
   `uv.lock`) do not block the branch switch. Keep unrelated working-tree
   noise (e.g. `.gitignore` tweaks) out of both branches.

## Automation

The same steps 4 + 7 run automatically via
`.github/workflows/pages.yml` on a published release or a manual
`workflow_dispatch` (it fetches the release, derives the page and pushes
`gh-pages` with the same commit message). The manual path above is the
fallback when the `gh` token is invalid — fix it with `gh auth login` to
reuse the workflow trigger.
