"""`bench` — the harness CLI. The system's single external seam."""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import pathlib
import sys

from . import (
    analyze,
    calibration,
    concurrency,
    cost,
    dataset_export,
    gate,
    predict,
    preflight,
    pricing_pull,
    releases,
    workloads,
)
from .pricing import PriceTable, TableError
from .runner import Manifest, RunnerError, run_level, status_doc

SUBCOMMANDS = (
    "dry-run",
    "run",
    "probe-concurrency",
    "calibrate-cache",
    "predict",
    "analyze",
    "status",
    "resume",
    "release",
    "dataset",
    "pricing-pull",
)


def _base(args: argparse.Namespace) -> pathlib.Path:
    return pathlib.Path(args.base).resolve()


def _pricing_dir(args: argparse.Namespace) -> pathlib.Path:
    """`--pricing-dir` resolves against `--base` when relative."""
    ruta = pathlib.Path(args.pricing_dir)
    return ruta if ruta.is_absolute() else _base(args) / ruta


def _emit(msg: str) -> None:
    print(msg, flush=True, file=sys.stderr)  # progress is log noise: stdout stays parseable


def _validate_scenario(args: argparse.Namespace) -> str | None:
    """Validates --s/--reps; returns the error message or None."""
    if not (0.0 <= args.s <= 1.0):
        return f"--s must be in [0, 1] (S1 cache hit-rate); got {args.s!r}"
    if args.reps < 1:
        return f"--reps must be >= 1; got {args.reps!r}"
    return None


def _validate_settle(args: argparse.Namespace) -> str | None:
    """Validates the registration settle's cap and poll; error message or None.

    Both must be strictly positive: a zero cap closes every bracket before the
    meter can register it (every dpp would read 0.0, the lane unproven), and a
    zero poll would hammer the meter. The poll must also sit below the cap —
    the cap is what bounds the wait, and a poll at or above it could never fit
    the two consecutive reads the loop's stability test needs.
    """
    if not math.isfinite(args.settle_s) or args.settle_s <= 0:
        return f"--settle-s must be a finite number > 0; got {args.settle_s!r}"
    if not math.isfinite(args.settle_poll_s) or args.settle_poll_s <= 0:
        return f"--settle-poll-s must be a finite number > 0; got {args.settle_poll_s!r}"
    if args.settle_poll_s >= args.settle_s:
        return (
            f"--settle-poll-s ({args.settle_poll_s!r}) must be smaller than --settle-s "
            f"({args.settle_s!r}): the cap bounds the registration loop, and a poll at "
            "or above it could never land the two consecutive reads stability needs"
        )
    return None


def _validate_run(args: argparse.Namespace, tabla: PriceTable) -> str | None:
    """Validates the run's parameters against the level's slate; error message or None."""
    if args.k < 1:
        return f"--k must be >= 1; got {args.k!r}"
    if args.reps < 1:
        return f"--reps must be >= 1; got {args.reps!r}"
    error = _validate_settle(args)
    if error:
        return error
    if args.rep is not None and not (1 <= args.rep <= args.reps):
        return f"--rep must be in [1, --reps={args.reps}]; got {args.rep!r}"
    if args.level == "T2" and args.rep is not None:
        # The hybrid composition (methodology v1.1 §5) pools every rep of a
        # cell into one bracket: there is no per-rep bracket to narrow to, and
        # a --rep plan would re-bill that rep's requests under new batch ids.
        return (
            "--rep has no per-rep bracket to run on T2: the hybrid composition pools "
            "every rep of a cell into one bracket (the strong four per-cell, the weak "
            "trio pooled per model) - run the full density (drop --rep)"
        )
    modelos = workloads.slate(args.level, tabla)
    if args.model is not None and args.model not in modelos:
        return f"--model {args.model!r} is not in the {args.level} slate ({len(modelos)} models)"
    return None


def cmd_dry_run(args: argparse.Namespace) -> int:
    if args.level is None:
        print("error: dry-run requires --level", file=sys.stderr)
        return 2
    error = _validate_scenario(args)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    try:
        tabla = PriceTable.load(_pricing_dir(args), args.table_version)
        filas = cost.budget(args.level, tabla, reps=args.reps, s=args.s)
    except (TableError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    estimado = {
        "table_version": tabla.table_version,
        "level": args.level,
        "reps": args.reps,
        "s": args.s,
        "rows": [dataclasses.asdict(f) for f in filas],
        "canary": cost.canary_estimate(),
    }
    if args.json:
        print(json.dumps(estimado, ensure_ascii=False, indent=2))
    else:
        print(f"table_version={tabla.table_version} level={args.level} S1 hit-rate={args.s}")
        print(
            f"{'workload':<20}{'models':>8}{'reps':>6}{'requests':>10}"
            f"{'tok_in':>11}{'tok_out':>9}{'nonce':>8}{'$ S0':>9}{'$ S1':>9}{'pp':>9}"
        )
        for f in filas:
            pp = "unmeasured" if f.pp_expected is None else f"{f.pp_expected:.4f}"
            print(
                f"{f.workload:<20}{f.models:>8}{f.reps:>8}{f.requests:>10,}"
                f"{f.tokens_in:>10,}{f.tokens_out:>9,}{f.nonce_tokens:>8,}"
                f"{f.cost_s0:>10.4f}{f.cost_s1:>9.4f}{pp:>9}"
            )
        print(
            f"{'TOTAL ' + args.level:<20}{'':>8}{'':>8}"
            f"{sum(f.requests for f in filas):>10,}"
            f"{sum(f.tokens_in for f in filas):>10,}{sum(f.tokens_out for f in filas):>9,}"
            f"{sum(f.nonce_tokens for f in filas):>8,}"
            f"{sum(f.cost_s0 for f in filas):>10.4f}{sum(f.cost_s1 for f in filas):>9.4f}"
        )
        canario = estimado["canary"]
        print(
            f"billing canary (once per run): ~{canario['requests']} requests, "
            f"~{canario['tokens_estimate']:,} tokens - the cache-free lane's gate check"
        )
    gate.mark_dry_run(_base(args), args.level, estimado)
    return 0


def _preflight_line(catalogo: preflight.CatalogReport) -> str:
    """The one-line preflight report (stderr): slate coverage, tag mappings, new ids."""
    partes = [f"preflight: catalog ok - {len(catalogo.matched)} slate ids present in /v1/models"]
    por_tag = sorted((s, c) for s, c in catalogo.matched.items() if s != c)
    if por_tag:
        partes.append("by tag: " + ", ".join(f"{s} -> {c}" for s, c in por_tag))
    for slate_id, variantes in sorted(catalogo.ambiguous.items()):
        partes.append(
            f"AMBIGUOUS: {slate_id} matches {len(variantes)} catalog variants "
            f"({', '.join(variantes)}); billing the first ({variantes[0]})"
        )
    if catalogo.unseen:
        partes.append("new in catalog (not in the price table): " + ", ".join(catalogo.unseen))
    return "; ".join(partes)


def _catalog_kwargs(catalogo: preflight.CatalogReport) -> dict:
    """The runner's catalog/model_map kwargs, shared by every spending command."""
    return {
        "catalog": {"http": catalogo.http, "ids": catalogo.ids, "matched": catalogo.matched},
        "model_map": {s: c for s, c in catalogo.matched.items() if c != s},
    }


def _spend_command(
    args: argparse.Namespace, *, nivel: str, slate_ids: list[str], tabla: PriceTable, run
) -> tuple[int, dict]:
    """The skeleton of the three quota-spending commands: consume the dry-run
    mark (require already passed), preflight the slate against the live
    catalog, run the workstream, and print the parseable JSON tail on success.
    Returns (exit_code, resumen) — resumen is {} on failure; the caller renders
    its own human report.

    The mark is consumed before preflight on purpose: the require->consume
    window stays a pair of adjacent filesystem ops (a concurrent run can never
    double the approved spend), and an aborted preflight only costs a fresh
    (free) dry-run.
    """
    gate.consume(_base(args), nivel)  # one dry-run enables exactly one invocation
    try:
        catalogo = preflight.verify(slate_ids=slate_ids, table_models=tabla.models)
    except preflight.PreflightError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1, {}
    _emit(_preflight_line(catalogo))
    try:
        resumen = run(catalogo)
    except RunnerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1, {}
    if args.json:
        print(json.dumps(resumen, ensure_ascii=False, indent=2))
    return 0, resumen


def cmd_run(args: argparse.Namespace) -> int:
    if args.level is None:
        print(f"error: {args.comando} requires --level", file=sys.stderr)
        return 2
    try:
        tabla = PriceTable.load(_pricing_dir(args), args.table_version)
        gate.require_dry_run(
            _base(args), args.level, table_version=tabla.table_version, reps=args.reps
        )
        error = _validate_run(args, tabla)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    except (TableError, gate.GateClosed) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not _require_api_key():
        return 2
    modelos = [args.model] if args.model else workloads.slate(args.level, tabla)
    # The preflight check covers the models THIS run will bill: --model
    # narrowed the slate, so drift in a model the run never touches must not
    # abort it.
    codigo, resumen = _spend_command(
        args,
        nivel=args.level,
        slate_ids=modelos,
        tabla=tabla,
        run=lambda catalogo: run_level(
            _base(args),
            level=args.level,
            workloads=workloads.WORKLOADS_BY_LEVEL[args.level],
            models=modelos,
            reps=args.reps,
            rep_filter=args.rep,
            k=args.k,
            settle_s=args.settle_s,
            settle_poll_s=args.settle_poll_s,
            table_version=tabla.table_version,
            **_catalog_kwargs(catalogo),
            emit=_emit,
        ),
    )
    if codigo:
        return codigo
    if not args.json:
        print(
            f"{args.comando} {resumen['run_id']}: {resumen['batches_done']}/{resumen['batches_planned']} "
            f"batches done, {resumen['batches_skipped_done']} skipped, "
            f"{resumen['batches_in_flight_skipped']} in_flight skipped, "
            f"{resumen['batches_aborted_skipped']} aborted skipped, "
            f"{resumen['requests_written']} requests written"
        )
    return 0


def _stub(nombre: str):
    def _cmd(args: argparse.Namespace) -> int:
        print(f"`bench {nombre}` not implemented yet", file=sys.stderr)
        return 3

    return _cmd


def _print_status(doc: dict) -> None:
    counts = doc["counts"]
    print(f"{doc['level']} run {doc['run_id']}  table={doc['table_version']}  k={doc['k']}")
    print(
        f"  batches: {doc['planned']} planned | {counts['done']} done, "
        f"{counts['aborted']} aborted, {counts['in_flight']} in_flight, "
        f"{counts['pending']} pending"
    )
    print(f"  requests ok: {doc['requests_ok']}")
    quota = doc["quota"]
    print(
        f"  quota consumed: session {quota['dpp_session']:.1f} pp | "
        f"weekly {quota['dpp_weekly']:.1f} pp "
        f"({quota['batches_with_bracket']}/{quota['closed_batches']} closed batches with a "
        "readable bracket)"
    )
    if quota.get("canary_dpp_weekly") is not None or quota.get("canary_dpp_session") is not None:
        print(
            f"  billing canary spend (not a batch line): session "
            f"{_fmt(quota.get('canary_dpp_session'), ' pp')} | "
            f"weekly {_fmt(quota.get('canary_dpp_weekly'), ' pp')}"
        )
    atencion = {
        s: c
        for s, c in counts.items()
        if s not in ("done", "pending") and c  # aborted, in_flight, corrupt, anything unknown
    }
    if atencion:
        print("  attention: " + ", ".join(f"{c} {s}" for s, c in sorted(atencion.items())))
        for b in doc["batches"]:
            if b["status"] in ("done",):
                continue
            carga = b["workload"]
            if not carga:
                if isinstance(b.get("pool"), list):
                    carga = "pool[" + "+".join(b["pool"]) + "]"
                else:
                    carga = "?"
            coordenada = f"{carga}/{b['model'] or '?'}"
            if b.get("rep"):
                coordenada += f" rep{b['rep']}"
            print(f"    {b['status']}: {coordenada} [{str(b['batch_id'])[:12]}]")


def cmd_pricing_pull(args: argparse.Namespace) -> int:
    """`bench pricing-pull`: snapshots the upstream rate card into a new
    versioned price table. Maintenance, zero API quota: it fetches the
    published catalog artifact (never ollama.com), diffs it rate-by-rate
    against the latest local table and lands a new `pricing/<version>.json`.
    It never overwrites a landed table and never re-prices past data: the new
    table only reaches verdicts through the next run's own vintage."""
    try:
        informe = pricing_pull.pull(args.url, _pricing_dir(args), check=args.check)
    except pricing_pull.PullError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(informe, ensure_ascii=False, indent=2))
        return 0
    print(f"pricing-pull: {informe['table_version']} (generated_at {informe['generated_at']})")
    print(f"  source: {informe['source_url']}")
    if informe["latest"]:
        print(f"  diff vs {informe['latest']}:")
    else:
        print("  no local table yet; every model is new:")
    for modelo in informe["changes"]["added"]:
        print(f"    + {modelo}")
    for modelo in informe["changes"]["removed"]:
        print(f"    - {modelo}")
    for cambio in informe["changes"]["updated"]:
        viejo, nuevo = cambio["old"], cambio["new"]
        print(
            f"    ~ {cambio['model']}: input {viejo['input']}->{nuevo['input']}, "
            f"cached_input {viejo['cached_input']}->{nuevo['cached_input']}, "
            f"output {viejo['output']}->{nuevo['output']}"
        )
    for nota in informe["notes"]:
        print(f"  note: {nota}")
    if informe["up_to_date"]:
        print(f"up to date: {informe['latest']} already carries these rates")
        return 0
    if args.check:
        print("check only: nothing written")
        return 0
    print(f"wrote {informe['path']} ({informe['models']} models)")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Progress + consumed quota per level, read from the manifests alone."""
    runs_dir = _base(args) / "runs"
    if args.level is not None:
        niveles = [args.level]
    else:
        niveles = (
            sorted(
                ruta.name[len("manifest-") : -len(".json")]
                for ruta in runs_dir.glob("manifest-*.json")
            )
            if runs_dir.exists()
            else []
        )
    if not niveles:
        print("no run manifests: nothing has run yet", file=sys.stderr)
    resumen = []
    for nivel in niveles:
        try:
            manifiesto = Manifest.load(runs_dir / f"manifest-{nivel}.json")
        except RunnerError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if manifiesto is None:
            # stderr: stdout carries only the report, so --json stays parseable
            print(f"{nivel}: no run manifest - nothing has run for this level", file=sys.stderr)
            continue
        resumen.append(status_doc(nivel, manifiesto))
    if args.json:
        print(json.dumps({"levels": resumen}, ensure_ascii=False, indent=2))
    else:
        for i, doc in enumerate(resumen):
            if i:
                print()
            _print_status(doc)
    return 0


def _require_api_key() -> bool:
    """False when the key is missing (the guard's message is printed)."""
    if os.environ.get("OLLAMA_API_KEY"):
        return True
    print(
        "error: OLLAMA_API_KEY is not set - the harness reads the key only from the "
        "environment and never writes it to any dataset",
        file=sys.stderr,
    )
    return False


def _fmt_cost(valor: float | None) -> str:
    """A per-task cost for the human report; the metric is null when unmeasurable."""
    return "n/a" if valor is None else f"${valor:.6f}"


def _fmt(valor: float | None, sufijo: str = "") -> str:
    """A nullable metric for the human report (dpp, wall-clock)."""
    return "n/a" if valor is None else f"{valor:g}{sufijo}"


def cmd_probe_concurrency(args: argparse.Namespace) -> int:
    """The concurrency workstream: cut-off probe + re-anchored k∈{1,4,8} cells."""
    if args.level is not None and args.level != "T1":
        print(
            "error: probe-concurrency runs on the T1 anchor only (its cell is the "
            "calibration fixture); the gate mark it consumes is T1's",
            file=sys.stderr,
        )
        return 2
    if not args.model:
        print(
            "error: probe-concurrency requires --model (the cells run on one model)",
            file=sys.stderr,
        )
        return 2
    if not concurrency.PROBE_K_FROM <= args.k_max <= concurrency.PROBE_K_CEILING:
        print(
            f"error: --k-max must be in [{concurrency.PROBE_K_FROM}, "
            f"{concurrency.PROBE_K_CEILING}] (the probe floor and spend ceiling); "
            f"got {args.k_max!r}",
            file=sys.stderr,
        )
        return 2
    error_settle = _validate_settle(args)
    if error_settle:
        print(f"error: {error_settle}", file=sys.stderr)
        return 2
    if not math.isfinite(args.ancla) or args.ancla <= 0:
        print(f"error: --ancla must be a finite number > 0; got {args.ancla!r}", file=sys.stderr)
        return 2
    try:
        tabla = PriceTable.load(_pricing_dir(args), args.table_version)
        if args.model not in tabla.models:
            print(
                f"error: --model {args.model!r} is not in the price table "
                f"{tabla.table_version!r} ({len(tabla.models)} models)",
                file=sys.stderr,
            )
            return 2
        gate.require_dry_run(_base(args), "T1", table_version=tabla.table_version)
    except (TableError, gate.GateClosed) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not _require_api_key():
        return 2

    codigo, resumen = _spend_command(
        args,
        nivel="T1",
        slate_ids=[args.model],
        tabla=tabla,
        run=lambda catalogo: concurrency.run_probe(
            _base(args),
            model=args.model,
            k_max=args.k_max,
            settle_s=args.settle_s,
            settle_poll_s=args.settle_poll_s,
            ancla=args.ancla,
            table_version=tabla.table_version,
            **_catalog_kwargs(catalogo),
            emit=_emit,
        ),
    )
    if codigo:
        return codigo
    if args.json:
        return 0
    corte = resumen["probe"]["cut_off"]
    corte_txt = "below the probe floor" if corte is None else str(corte)
    print(
        f"concurrency run {resumen['run_id']} - models {', '.join(resumen['models']) or '(none)'}, "
        f"anchor ${resumen['ancla']:g}/mo ({resumen['usd_per_pp']:.6f} USD/pp)"
    )
    print(f"  cut-off: {corte_txt} - {resumen['probe']['cut_off_note']}")
    modelo_actual = None
    for celda in resumen["cells"]:
        if celda["model"] != modelo_actual:  # the doc covers every model of the run
            modelo_actual = celda["model"]
            print(f"  model {modelo_actual}:")
        print(
            f"    k={celda['k']}"
            + (" (re-anchored)" if celda["re_anchored"] else "")
            + f": {celda['completed']}/{celda['n']} completed, "
            f"dpp_weekly={_fmt(celda['dpp_weekly'], ' pp')}, "
            f"wall={_fmt(celda['wall_clock_s'], 's')}, "
            f"{_fmt_cost(celda['cost_per_attempted_task_usd'])}/task attempted"
            + (
                f", {_fmt_cost(celda['cost_per_completed_task_usd'])}/task completed"
                if celda["cost_per_completed_task_usd"] is not None
                else ""
            )
        )
        if celda["notes"]:
            print(f"        {celda['notes']}")
    return 0


def cmd_calibrate_cache(args: argparse.Namespace) -> int:
    """The cache calibration (methodology v1 §7): prefix replays per T2-slate model.

    The gate consumes the T2 mark: the calibration bills T2-density requests
    (the ~20K prefix), far below the full level the mark approved.
    """
    if args.level is not None:
        print(
            "error: calibrate-cache runs on the T2 slate; it takes no --level",
            file=sys.stderr,
        )
        return 2
    edades = tuple(float(a) for a in args.spaced_gaps)
    if any(not math.isfinite(a) or a < 0 for a in edades) or not (
        edades[0] < edades[1] < edades[2]
    ):
        print(
            "error: --spaced-gaps must be three strictly increasing finite "
            f"offsets in seconds; got {args.spaced_gaps!r}",
            file=sys.stderr,
        )
        return 2
    error_settle = _validate_settle(args)
    if error_settle:
        print(f"error: {error_settle}", file=sys.stderr)
        return 2
    try:
        tabla = PriceTable.load(_pricing_dir(args), args.table_version)
        slate = workloads.slate("T2", tabla)
        modelos = [args.model] if args.model else list(slate)
        fuera = [m for m in modelos if m not in slate]
        if fuera:
            print(
                f"error: --model {', '.join(fuera)} is not in the T2 slate ({len(slate)} models)",
                file=sys.stderr,
            )
            return 2
        gate.require_dry_run(_base(args), "T2", table_version=tabla.table_version)
    except (TableError, gate.GateClosed) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not _require_api_key():
        return 2

    codigo, resumen = _spend_command(
        args,
        nivel="T2",
        slate_ids=modelos,
        tabla=tabla,
        run=lambda catalogo: calibration.run_calibration(
            _base(args),
            models=modelos,
            spaced_ages=edades,
            settle_s=args.settle_s,
            settle_poll_s=args.settle_poll_s,
            table_version=tabla.table_version,
            tabla=tabla,
            **_catalog_kwargs(catalogo),
            emit=_emit,
        ),
    )
    if codigo:
        return codigo
    if args.json:
        return 0
    lecturas = resumen["readings"]
    print(
        f"cache calibration {resumen['run_id']} - models "
        f"{', '.join(resumen['models']) or '(none)'}, table {resumen['table_version']}"
    )
    for modelo in resumen["models"]:
        a = lecturas[modelo]
        tasa = "n/a" if a["hit_rate"] is None else f"{a['hit_rate'] * 100:.1f}%"
        base_est = f" ({a['hit_rate_basis']})" if a["hit_rate_basis"] else ""
        descuento = a["paper_discount"]
        declarado = "declared" if descuento["declared"] else "none in the table"
        if descuento["materialized"] is None:
            materializado = "unknown"
        else:
            materializado = "materialized" if descuento["materialized"] else "NOT materialized"
        print(
            f"  {modelo}: cache {a['cache_exists']}, persistence "
            f"{a['persistence'] or 'unknown'}, hit rate {tasa}{base_est}, "
            f"{'conclusive' if a['conclusive'] else 'inconclusive'} - "
            f"paper discount {declarado}, {materializado}"
        )
    if resumen["unmaterialized_paper_discounts"]:
        print(
            "  unmaterialized paper discounts: "
            + ", ".join(resumen["unmaterialized_paper_discounts"])
        )
    return 0


def _pct(valor: float | None) -> str:
    """A MAPE for the human report (a ratio rendered as a percentage)."""
    return "n/a" if valor is None else f"{valor * 100:.1f}%"


def _ci_pct(ci: list | None) -> str:
    if not ci:
        return "n/a"
    return f"[{_pct(ci[0])} - {_pct(ci[1])}]"


def _print_predict_report(doc: dict, ruta: pathlib.Path) -> int:
    """The report's human summary: the two phases' comparative verdicts + findings."""
    print(
        f"predictability report (table {doc['table_version']}): "
        f"{doc['estimates']['blind']} blind, {doc['estimates']['informed']} informed estimates"
    )
    for fase in ("blind", "informed"):
        agregado = doc["aggregate"].get(fase)
        if agregado is None:
            print(f"  {fase}: no estimates recorded")
            continue
        legado, nuevo = agregado["mape_legacy"], agregado["mape_new"]
        print(
            f"  {fase}: MAPE legacy {_pct(legado['mape'] if legado else None)} "
            f"{_ci_pct(legado['ci'] if legado else None)} | "
            f"MAPE new {_pct(nuevo['mape'] if nuevo else None)} "
            f"{_ci_pct(nuevo['ci'] if nuevo else None)} "
            f"({nuevo['cells'] if nuevo else 0} cells)"
        )
        print(
            f"    paired delta (legacy - new): {_pct(agregado['delta_mape'])} "
            f"{_ci_pct(agregado['ci_delta'])} ({agregado['paired_cells']} paired cells) - "
            f"{agregado['verdict']}; Ollama's claim: {agregado['ollama_claim']}"
        )
    hallazgos = doc["findings"]
    if hallazgos["sub_resolution_legacy"]:
        print(
            "  sub-resolution (excluded from the legacy side): "
            + "; ".join(hallazgos["sub_resolution_legacy"])
        )
    if hallazgos["pending_blind"]:
        print(
            f"  pending blind estimates: {len(hallazgos['pending_blind'])} cells "
            "(the flow refuses them once the cell has run)"
        )
    if hallazgos["measured_without_blind"]:
        print(
            "  measured with no blind estimate (that cell can never join the study): "
            + "; ".join(hallazgos["measured_without_blind"])
        )
    if hallazgos["off_grid_estimates"]:
        print(
            "  estimates outside the current grid (retired scope, counted nowhere): "
            + "; ".join(hallazgos["off_grid_estimates"])
        )
    print(f"  report: {ruta}")
    return 0


def _predict_report(args: argparse.Namespace, tabla: PriceTable) -> int:
    try:
        doc = predict.build_report(_base(args), tabla=tabla)
    except (predict.PredictError, analyze.AnalyzeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    ruta = pathlib.Path(_base(args)) / predict.PREDICT_DIR / "report.json"
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0
    return _print_predict_report(doc, ruta)


def _predict_record(args: argparse.Namespace, tabla: PriceTable) -> int:
    try:
        linea = predict.record_estimate(
            _base(args),
            phase=args.phase,
            workload=args.workload,
            model=args.model,
            estimated_pp=args.pp,
            estimated_usd=args.usd,
            notes=args.notes,
            tabla=tabla,
        )
    except predict.PredictError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(linea, ensure_ascii=False, indent=2))
        return 0
    print(
        f"locked: {linea['phase']} estimate for {args.workload}/{args.model} - "
        f"{linea['estimated_pp']:g} pp weekly, ${linea['estimated_usd']:g} credits "
        f"(table {linea['table_version']}, hash {str(linea['hash'])[:12]})"
    )
    return 0


def _predict_walkthrough(args: argparse.Namespace, tabla: PriceTable) -> int:
    # The walk-through: the grid's state plus the pending cells' public brief.
    try:
        doc = predict.plan_doc(_base(args), tabla)
    except (predict.PredictError, TableError) as e:
        # a table that no longer prices a grid model: clean refusal
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0
    counts = doc["counts"]
    print(
        f"predictability: table {doc['table_version']} - {counts['cells']} cells, "
        f"{counts['blind']} blind, {counts['informed']} informed"
    )
    if counts["off_grid"]:
        print(
            f"  warning: {counts['off_grid']} locked estimates belong to cells outside "
            "the current grid (a retired scope) - they are counted nowhere and the "
            "report flags them in findings.off_grid_estimates",
            file=sys.stderr,
        )
    for fila in doc["cells"]:
        etiqueta = f"{fila['workload']}/{fila['model']} [{fila['level']}]"
        if fila["blind"] is None:
            b = fila["brief"]
            print(f"  {etiqueta} - PENDING blind: {b['description']}")
            print(
                f"      {b['requests_per_run']} requests/run, ~{b['tokens_in_per_request']:,} in / "
                f"~{b['tokens_out_per_request']:,} out per request"
                f" (+~{b['nonce_tokens_per_request']:,} nonce in, cache-free lane)"
                f" | rates: input "
                f"${b['rates']['input']:g}, cached ${b['rates']['cached_input']:g}, output "
                f"${b['rates']['output']:g} per {b['rates']['per']:,}"
                + (" (cache discount)" if b["cache_discount"] else " (cached=input)")
            )
            print(
                f"      estimate: bench predict --phase blind --workload {fila['workload']} "
                f"--model {fila['model']} --pp <weekly pp> --usd <credits $>"
            )
        elif fila["informed"] is None:
            c = fila["blind"]
            print(
                f"  {etiqueta} - blind locked ({c['estimated_pp']:g} pp, ${c['estimated_usd']:g}); "
                "PENDING informed"
            )
        else:
            c = fila["informed"]
            print(
                f"  {etiqueta} - done (blind {fila['blind']['estimated_pp']:g} pp / "
                f"${fila['blind']['estimated_usd']:g}, informed {c['estimated_pp']:g} pp / "
                f"${c['estimated_usd']:g})"
            )
    return 0


def cmd_predict(args: argparse.Namespace) -> int:
    """The predictability HITL flow (methodology v1 §8). It never touches the API:
    the estimates are recorded from the owner's judgment against the fixture's
    public description and the rate table, and the report re-derives everything
    offline from the raw datasets, like analyze."""
    if args.level is not None:
        print(
            "error: predict runs on the predictability grid; it takes no --level",
            file=sys.stderr,
        )
        return 2
    grabando = args.phase is not None
    if args.report and grabando:
        print("error: give either --report or --phase, not both", file=sys.stderr)
        return 2
    grabadoras = (args.workload, args.model, args.pp, args.usd, args.notes)
    if not (args.report or grabando) and any(v not in (None, "") for v in grabadoras):
        print(
            "error: --workload/--model/--pp/--usd/--notes record an estimate; "
            "give --phase blind|informed (or --report)",
            file=sys.stderr,
        )
        return 2
    if args.report and any(v not in (None, "") for v in grabadoras):
        print(
            "error: the report covers the whole grid; it takes no recording flags "
            "(--workload/--model/--pp/--usd/--notes)",
            file=sys.stderr,
        )
        return 2
    if grabando and (args.workload is None or args.model is None):
        print(
            "error: recording an estimate needs both --workload and --model (the cell)",
            file=sys.stderr,
        )
        return 2
    if grabando and (args.pp is None or args.usd is None):
        print(
            "error: recording an estimate needs both --pp (weekly pp, legacy) and "
            "--usd (dollars of credits, new) - the estimate is in native units",
            file=sys.stderr,
        )
        return 2
    try:
        tabla = PriceTable.load(_pricing_dir(args), args.table_version)
    except TableError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.report:
        return _predict_report(args, tabla)
    if grabando:
        return _predict_record(args, tabla)
    return _predict_walkthrough(args, tabla)


def _print_analyze(doc: dict, carpeta: pathlib.Path, etiqueta: str | None = None) -> None:
    """The analyze human report: the baseline params, the verdict census, the bundle."""
    bp = doc["base_params"]
    encabezado = "analysis" + (f" - {etiqueta}" if etiqueta else "")
    print(
        f"{encabezado}: table={bp['table_version']} ancla={bp['ancla']:g} "
        f"({bp['usd_per_pp']:.6f} USD/pp) s={bp['s']} | raw: "
        f"{doc['raw']['request_lines']} requests, {doc['raw']['batch_lines']} batches"
    )
    conteo = {"legacy": 0, "new": 0, "tie": 0, "no data": 0}
    for c in doc["cells"]:
        conteo[c["verdict"]["s0"]["winner"]] += 1
    print(
        f"  cells: {len(doc['cells'])} | s0 verdicts: {conteo['legacy']} legacy, "
        f"{conteo['new']} new, {conteo['tie']} tie, {conteo['no data']} no data"
    )
    if doc["paper_discounts"]:
        print("  unmaterialized paper discounts: " + ", ".join(doc["paper_discounts"]))
    print(f"  bundle: {carpeta} (analysis.json, dashboard.html, calculator.html)")


def _analyze_release(args: argparse.Namespace) -> int:
    """`analyze --release <tag>`: fetch the dataset release, verify it against
    its metadata's sha256 map, and analyze it with the release's OWN table —
    the raw<->code<->table pairing, consumed. Still offline against the API:
    only `gh` moves bytes."""
    base = _base(args)

    if args.pricing_dir != "pricing":
        _emit("note: --pricing-dir is ignored with --release (the release carries its own table)")
    try:
        repo = args.repo or releases.infer_repo(base)
        # fetch() refuses (before touching the previous fetch) when the
        # requested table version, level or model is not what the release
        # carries: a silent empty analysis would read as a verdict of none.
        stage, meta = releases.fetch(
            base,
            tag=args.release,
            repo=repo,
            table_version=args.table_version,
            level=args.level,
            model=args.model,
        )
        tabla = releases.release_table(stage)
    except (releases.ReleaseError, TableError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        doc = analyze.build(
            stage,
            tabla=tabla,
            ancla=args.ancla,
            s=args.s,
            level=args.level,
            model=args.model,
            protocol_version=meta.get("protocol_version"),
            credit_ratio=args.credit_ratio,
        )
    except analyze.AnalyzeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        carpeta = analyze.write_bundle(stage, doc, tabla=tabla)
    except analyze.AnalyzeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0
    _print_analyze(doc, carpeta, etiqueta=f"release {args.release}")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    """The re-run without re-measuring: the whole bundle from raw alone.

    The API key is never needed here and never read: analyze works offline on
    the immutable datasets, so a price change (or a new anchor or S1 guess)
    re-derives every derived number with zero quota spent. `--release <tag>`
    points it at a fetched dataset release instead of the local raw data.
    """
    if not (0.0 <= args.s <= 1.0):
        print(f"error: --s must be in [0, 1] (S1 cache hit-rate); got {args.s!r}", file=sys.stderr)
        return 2
    if not math.isfinite(args.ancla) or args.ancla <= 0:
        print(f"error: --ancla must be a finite number > 0; got {args.ancla!r}", file=sys.stderr)
        return 2
    if not math.isfinite(args.credit_ratio) or args.credit_ratio < 1:
        print(
            f"error: --credit-ratio must be a finite number >= 1 (the tiers sell "
            f"credits at or above face value: Team x2, Pro/Max x3); got {args.credit_ratio!r}",
            file=sys.stderr,
        )
        return 2
    if args.release is not None:
        return _analyze_release(args)
    try:
        tabla = PriceTable.load(_pricing_dir(args), args.table_version)
    except TableError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        doc = analyze.build(
            _base(args),
            tabla=tabla,
            ancla=args.ancla,
            s=args.s,
            level=args.level,
            model=args.model,
            credit_ratio=args.credit_ratio,
        )
    except analyze.AnalyzeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        carpeta = analyze.write_bundle(_base(args), doc, tabla=tabla)
    except analyze.AnalyzeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0
    _print_analyze(doc, carpeta)
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    """Dataset sync to GitHub releases (methodology v1 §4): one release per
    run, pairing raw<->code<->table. It never touches the ollama API and never
    reads the key for anything but the credential scrub."""
    if not args.run:
        print("error: release requires --run <run_id>", file=sys.stderr)
        return 2
    base = _base(args)
    try:
        repo = args.repo or releases.infer_repo(base)
        paquete = releases.package(base, run_id=args.run, pricing_dir=_pricing_dir(args))
        releases.publish(base, paquete, repo=repo)
    except releases.ReleaseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(
            json.dumps(
                {
                    "tag": paquete.tag,
                    "repo": repo,
                    "tar": str(paquete.tar),
                    "metadata_asset": str(paquete.metadata),
                    "metadata": paquete.doc,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    meta = paquete.doc
    print(f"released {paquete.tag} -> {repo}")
    print(
        f"  run {paquete.run_id} (level {meta.get('level')}) - table {meta['table_version']}, "
        f"protocol {meta['protocol_version']}, {meta['counts']['request_lines']} request lines, "
        f"{meta['counts']['batch_lines']} batch lines"
    )
    commit = (meta.get("code") or {}).get("git_commit")
    print(f"  code: {commit or 'unknown commit (not a git checkout)'}")
    print("  assets: dataset.tar.gz, metadata.json, notes.md")
    return 0


def cmd_dataset(args: argparse.Namespace) -> int:
    """`bench dataset --release <tag>`: the readable copy of a dataset release
    (JSON/CSV/Excel). It fetches and verifies the release against its metadata's
    sha256 map, then flattens its raw evidence — zero quota, no API, and the
    published release is never rewritten: this regenerates derivatives."""
    base = _base(args)
    try:
        repo = args.repo or releases.infer_repo(base)
        stage, meta = releases.fetch(base, tag=args.release, repo=repo)
        destino = (
            pathlib.Path(args.out).resolve()
            if args.out
            else base / releases.RELEASES_DIR / f"export-{meta['run_id']}"
        )
        archivos = [(rel, stage / rel) for rel in sorted(meta["files"])]
        encabezado = {
            "run_id": meta["run_id"],
            "level": meta.get("level"),
            "models": meta.get("models"),
            "protocol_version": meta.get("protocol_version"),
            "table_version": meta.get("table_version"),
        }
        escritos = dataset_export.export_dataset(destino, archivos, header=encabezado)
    except (releases.ReleaseError, TableError, dataset_export.ExportError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(
            json.dumps(
                {
                    "tag": args.release,
                    "repo": repo,
                    "out": str(destino),
                    "files": [str(p) for _, p in escritos],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(f"dataset {args.release} -> {destino}")
    for _, ruta in escritos:
        print(f"  {ruta}")
    return 0


DESPACHO = {
    "dry-run": cmd_dry_run,
    "run": cmd_run,
    "probe-concurrency": cmd_probe_concurrency,
    "calibrate-cache": cmd_calibrate_cache,
    "predict": cmd_predict,
    "analyze": cmd_analyze,
    "status": cmd_status,
    # resume IS run against the run's manifest: batch_id is deterministic from
    # (run, level, workload, model, rep, k), so a re-invocation resumes done
    # batches without re-billing and skips aborted/in_flight ones loudly.
    "resume": cmd_run,
    "release": cmd_release,
    "dataset": cmd_dataset,
    "pricing-pull": cmd_pricing_pull,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bench", description="Cost benchmark harness for Ollama Cloud")
    p.add_argument("--base", default=".", help="working directory (pricing/, runs/)")
    sub = p.add_subparsers(dest="comando", required=True)
    for nombre in SUBCOMMANDS:
        # allow_abbrev off for the probe: `--k 8` would otherwise prefix-match
        # --k-max and silently raise the probe's spend ceiling instead of erroring
        parser = sub.add_parser(nombre, allow_abbrev=(nombre != "probe-concurrency"))
        if nombre == "status":
            # status also reports the workstreams' manifests (e.g. T1-concurrency):
            # free-form, filtered by manifest file name
            parser.add_argument("--level", default=None)
        elif nombre == "release":
            parser.add_argument("--run", required=True, help="the run_id to package and publish")
            parser.add_argument(
                "--repo", default=None, help="owner/name (default: git remote origin)"
            )
        elif nombre == "dataset":
            # dataset never tunes a spend or an assumption: it reads a verified
            # release and re-flattens its raw evidence. Knobs: --release, --repo,
            # --out only.
            parser.add_argument(
                "--release",
                required=True,
                metavar="TAG",
                help="the dataset release tag to fetch, verify and flatten (run-<run_id>)",
            )
            parser.add_argument(
                "--repo", default=None, help="owner/name (default: git remote origin)"
            )
            parser.add_argument(
                "--out",
                default=None,
                help="output directory (default: releases/export-<run_id> under --base)",
            )
        elif nombre == "pricing-pull":
            # The pull never tunes a spend or an assumption either: it fetches
            # the published rate card and lands a new snapshot. Knobs: --url
            # (the artifact source) and --check (diff only, nothing written).
            parser.add_argument(
                "--url",
                default=pricing_pull.DEFAULT_PRICING_URL,
                help="the upstream rate card to snapshot",
            )
            parser.add_argument(
                "--check",
                action="store_true",
                help="diff against the latest local table without writing",
            )
        else:
            parser.add_argument("--level", choices=["T1", "T2", "T3"], default=None)
        if nombre not in ("release", "dataset", "pricing-pull"):  # none touches a model
            parser.add_argument("--model", default=None)
        if nombre != "dataset":  # a release pairs its dataset with its own table
            parser.add_argument(
                "--pricing-dir", default="pricing", help="tables directory (relative to --base)"
            )
        if nombre not in ("release", "dataset", "pricing-pull"):  # the manifest binds the table;
            # a pull derives its own version from the upstream generated_at, never an override
            parser.add_argument("--table-version", default=None)
        if nombre == "probe-concurrency":
            # The probe reads none of --s/--reps/--rep/--k: a silent no-op flag
            # would read as a tuned cell (--k) or an approved density (--reps)
            # it ignores. Its knobs are --model, --k-max and --ancla (the anchor
            # its cost-per-task verdict divides by); the cell ks are the
            # workstream's own (1, 4, 8), re-anchored to the measured cut-off.
            parser.add_argument(
                "--k-max",
                type=int,
                default=20,
                help=(
                    "the probe's ceiling k (the sweep is 4..k-max; default 20, "
                    f"hard ceiling {concurrency.PROBE_K_CEILING})"
                ),
            )
            parser.add_argument("--ancla", type=float, default=100.0, help="P_LEGADO USD/month")
        elif nombre == "calibrate-cache":
            # The calibration reads none of --s/--reps/--rep/--k/--ancla either:
            # a silent no-op flag would read as a tuned assumption. Its knobs
            # are --model and --spaced-gaps (the replays' offsets), with
            # --settle-s governing the brackets' settle as everywhere else.
            parser.add_argument(
                "--spaced-gaps",
                type=float,
                nargs=3,
                default=calibration.SPACED_TARGETS,
                metavar="S",
                help=(
                    "the spaced replays' cumulative offsets in seconds "
                    "(default 5 30 90; the ladder sits above the bracket's settle)"
                ),
            )
        elif nombre == "analyze":
            # analyze's own knobs: --ancla (the anchor its legacy dollars
            # divide by), --s (the S1 assumption it extrapolates with) and
            # --credit-ratio (the new plan's per-tier credit multiplier its
            # verdicts re-denominate by). No --reps/--rep/--k: those tune
            # SPENDING, and a silent no-op here would read as a re-measured
            # density instead of a re-priced bundle.
            parser.add_argument("--ancla", type=float, default=100.0, help="P_LEGADO USD/month")
            parser.add_argument(
                "--credit-ratio",
                type=float,
                default=3.0,
                help=(
                    "new-plan credits per paid dollar at the comparisons "
                    "(verdicts, margins, pp/1M threshold); the tiers are Team "
                    "x2, Pro/Max x3; 1 reproduces the legacy 1:1 credit "
                    "comparison"
                ),
            )
            parser.add_argument(
                "--s",
                type=float,
                default=analyze.S1_DEFAULT,  # the versioned default, never a second literal
                help="S1 cache hit-rate (0..1)",
            )
            parser.add_argument(
                "--release",
                default=None,
                metavar="TAG",
                help=(
                    "analyze a dataset release's fetched copy (releases/<tag>/) instead "
                    "of the local raw data; its own table prices it, so --pricing-dir "
                    "is ignored and --table-version must match the release's"
                ),
            )
            parser.add_argument(
                "--repo", default=None, help="owner/name for --release (default: git remote origin)"
            )
        elif nombre == "predict":
            # predict's own knobs: --phase (the flow's mode), the cell's
            # --workload, the estimate's native units (--pp weekly pp,
            # --usd credits) and --report. No --ancla: the MAPEs are
            # native-unit by decision, so the anchor never enters them; no
            # --reps/--rep/--k: predict never spends anything.
            parser.add_argument(
                "--phase",
                choices=[predict.BLIND, predict.INFORMED],
                default=None,
                help="record an estimate: blind (before the cell runs) or informed (after)",
            )
            parser.add_argument("--workload", default=None, help="the cell's workload")
            parser.add_argument("--pp", type=float, default=None, help="estimated weekly pp")
            parser.add_argument("--usd", type=float, default=None, help="estimated credits ($)")
            parser.add_argument("--notes", default="", help="the estimator's reasoning (locked)")
            parser.add_argument("--report", action="store_true", help="write the MAPE report")
            # No --s: the estimates and the comparative MAPE stay anchored to the
            # persisted S0/S1 pair (methodology v1.2) - a custom S(x) never
            # re-anchors them; it enters only through analyze's stamped re-runs.
        elif nombre in ("dry-run", "run", "resume", "status"):
            # status reads none of these but accepted them before Harness 10;
            # dropping them would break every script or habit mirroring `run`'s
            # invocation shape (a silent interface change, not a cleanup).
            parser.add_argument(
                "--s",
                type=float,
                default=analyze.S1_DEFAULT,  # the versioned default, never a second literal
                help="S1 cache hit-rate (0..1)",
            )
            parser.add_argument("--reps", type=int, default=5)
            parser.add_argument("--rep", type=int, default=None, help="run only this repetition")
            parser.add_argument("--k", type=int, default=1, help="concurrency of the burst")
        # release takes none of the above: it never tunes a spend or an
        # assumption. Its knobs are --run and --repo only.
        if nombre not in ("release", "dataset", "pricing-pull"):  # neither reads a settle
            # (neither brackets, and the pull never registers anything)
            parser.add_argument(
                "--settle-s",
                type=float,
                default=60.0,
                help=(
                    "registration cap (s): the settle polls /api/usage until two "
                    "consecutive reads agree in both windows, or this cap burns "
                    "(protocol v3; the fixed 90 s wait is dead)"
                ),
            )
            parser.add_argument(
                "--settle-poll-s",
                type=float,
                default=5.0,
                help="the registration loop's poll interval (s)",
            )
        parser.add_argument("--json", action="store_true")
        parser.set_defaults(func=DESPACHO.get(nombre, _stub(nombre)))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
