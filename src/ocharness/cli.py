"""`bench` — the harness CLI. The system's single external seam."""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import pathlib
import sys

from . import analyze, calibration, concurrency, cost, gate, preflight, workloads
from .pricing import PriceTable, TableError
from .runner import Manifest, RunnerError, run_level

SUBCOMMANDS = (
    "dry-run",
    "run",
    "probe-concurrency",
    "calibrate-cache",
    "analyze",
    "status",
    "resume",
)


def _base(args: argparse.Namespace) -> pathlib.Path:
    return pathlib.Path(args.base).resolve()


def _pricing_dir(args: argparse.Namespace) -> pathlib.Path:
    """`--pricing-dir` resolves against `--base` when relative."""
    ruta = pathlib.Path(args.pricing_dir)
    return ruta if ruta.is_absolute() else _base(args) / ruta


def _validate_scenario(args: argparse.Namespace) -> str | None:
    """Validates --s/--reps; returns the error message or None."""
    if math.isnan(args.s) or not (0.0 <= args.s <= 1.0):
        return f"--s must be in [0, 1] (S1 cache hit-rate); got {args.s!r}"
    if args.reps < 1:
        return f"--reps must be >= 1; got {args.reps!r}"
    return None


def _validate_run(args: argparse.Namespace, tabla: PriceTable) -> str | None:
    """Validates the run's parameters against the level's slate; error message or None."""
    if args.k < 1:
        return f"--k must be >= 1; got {args.k!r}"
    if args.reps < 1:
        return f"--reps must be >= 1; got {args.reps!r}"
    if args.settle_s < 0 or not math.isfinite(args.settle_s):
        return f"--settle-s must be a finite number >= 0; got {args.settle_s!r}"
    if args.rep is not None and not (1 <= args.rep <= args.reps):
        return f"--rep must be in [1, --reps={args.reps}]; got {args.rep!r}"
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
    }
    if args.json:
        print(json.dumps(estimado, ensure_ascii=False, indent=2))
    else:
        print(f"table_version={tabla.table_version} level={args.level} S1 hit-rate={args.s}")
        print(
            f"{'workload':<20}{'models':>8}{'reps':>6}{'requests':>10}"
            f"{'tok_in':>11}{'tok_out':>9}{'$ S0':>9}{'$ S1':>9}{'pp':>9}"
        )
        for f in filas:
            pp = "unmeasured" if f.pp_expected is None else f"{f.pp_expected:.4f}"
            print(
                f"{f.workload:<20}{f.models:>8}{f.reps:>8}{f.tokens_in:>10,}"
                f"{f.tokens_out:>9,}{f.cost_s0:>10.4f}{f.cost_s1:>9.4f}{pp:>9}"
            )
        print(
            f"{'TOTAL ' + args.level:<20}{'':>8}{'':>8}"
            f"{sum(f.tokens_in for f in filas):>10,}{sum(f.tokens_out for f in filas):>9,}"
            f"{sum(f.cost_s0 for f in filas):>10.4f}{sum(f.cost_s1 for f in filas):>9.4f}"
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


def cmd_run(args: argparse.Namespace) -> int:
    if args.level is None:
        print("error: run requires --level", file=sys.stderr)
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
    except TableError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except gate.GateClosed as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not os.environ.get("OLLAMA_API_KEY"):
        print(
            "error: OLLAMA_API_KEY is not set - the harness reads the key only from the "
            "environment and never writes it to any dataset",
            file=sys.stderr,
        )
        return 2
    modelos = [args.model] if args.model else workloads.slate(args.level, tabla)
    rep_filter = args.rep if args.rep else None

    def _emit(msg: str) -> None:
        print(msg, flush=True, file=sys.stderr)  # progress is log noise: stdout stays parseable

    gate.consume(_base(args), args.level)  # one dry-run enables exactly one run

    # Preflight before a single request is billed. The mark is consumed first:
    # the require->consume window stays a pair of adjacent filesystem ops (a
    # concurrent run can never double the approved spend), and an aborted
    # preflight only costs a fresh (free) dry-run. The check covers the models
    # THIS run will bill: --model narrowed the slate, so drift in a model the
    # run never touches must not abort it.
    try:
        catalogo = preflight.verify(slate_ids=modelos, table_models=tabla.models)
    except preflight.PreflightError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    _emit(_preflight_line(catalogo))

    try:
        resumen = run_level(
            _base(args),
            level=args.level,
            workloads=workloads.WORKLOADS_BY_LEVEL[args.level],
            models=modelos,
            reps=args.reps,
            rep_filter=rep_filter,
            k=args.k,
            settle_s=args.settle_s,
            table_version=tabla.table_version,
            catalog={"http": catalogo.http, "ids": catalogo.ids, "matched": catalogo.matched},
            model_map={s: c for s, c in catalogo.matched.items() if c != s},
            emit=_emit,
        )
    except RunnerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(resumen, ensure_ascii=False, indent=2))
    else:
        print(
            f"run {resumen['run_id']}: {resumen['batches_done']}/{resumen['batches_planned']} "
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


def _numero(valor) -> float | None:
    """A real number from a manifest (bools are not numbers here), or None."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    return valor


def _status_doc(nivel: str, manifiesto: Manifest) -> dict:
    """The status of one level's run, computed from its manifest (no API).

    The manifest is run state that a recovering operator may have hand-edited:
    malformed entries render as unknown/corrupt instead of crashing the report.
    """
    doc = manifiesto.doc
    counts: dict[str, int] = {"done": 0, "aborted": 0, "in_flight": 0}
    dpp_session = dpp_weekly = 0.0
    con_bracket = cerrados = 0
    requests_ok = 0
    batches = []
    for batch_id, entrada in doc.get("batches", {}).items():
        if not isinstance(entrada, dict):
            entrada = {"status": "corrupt"}
        estado = str(entrada.get("status", "?"))
        counts[estado] = counts.get(estado, 0) + 1
        dpp_s = _numero(entrada.get("dpp_session"))
        dpp_w = _numero(entrada.get("dpp_weekly"))
        if estado in ("done", "aborted"):
            cerrados += 1
        if dpp_s is not None and dpp_w is not None:
            dpp_session += dpp_s
            dpp_weekly += dpp_w
            con_bracket += 1
        ok = _numero(entrada.get("requests_ok"))
        if ok is not None:
            requests_ok += int(ok)
        batches.append(
            {
                "batch_id": batch_id,
                "status": estado,
                "workload": entrada.get("workload"),
                "model": entrada.get("model"),
                "rep": entrada.get("rep"),
                "dpp_session": dpp_s,
                "dpp_weekly": dpp_w,
                "requests_ok": None if ok is None else int(ok),
                "note": entrada.get("note"),
            }
        )
    try:
        planned = int(doc.get("planned", len(batches)))
    except (TypeError, ValueError):
        planned = len(batches)
    counts["pending"] = max(0, planned - len(batches))
    return {
        "level": doc.get("level", nivel),
        "run_id": doc.get("run_id"),
        "table_version": doc.get("table_version"),
        "protocol_version": doc.get("protocol_version"),
        "k": doc.get("k"),
        "started_at": doc.get("started_at"),
        "planned": planned,
        "counts": counts,
        "requests_ok": requests_ok,
        "quota": {
            # the meter quantizes at 0.1 pp: the sum carries no more precision
            "dpp_session": round(dpp_session, 4),
            "dpp_weekly": round(dpp_weekly, 4),
            "batches_with_bracket": con_bracket,
            "closed_batches": cerrados,
        },
        "batches": batches,
    }


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
            coordenada = f"{b['workload'] or '?'}/{b['model'] or '?'}"
            if b.get("rep"):
                coordenada += f" rep{b['rep']}"
            print(f"    {b['status']}: {coordenada} [{str(b['batch_id'])[:12]}]")


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
        resumen.append(_status_doc(nivel, manifiesto))
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
    if args.settle_s < 0 or not math.isfinite(args.settle_s):
        print(
            f"error: --settle-s must be a finite number >= 0; got {args.settle_s!r}",
            file=sys.stderr,
        )
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
    except TableError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except gate.GateClosed as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not _require_api_key():
        return 2

    def _emit(msg: str) -> None:
        print(msg, flush=True, file=sys.stderr)  # progress is log noise: stdout stays parseable

    gate.consume(_base(args), "T1")  # one dry-run enables exactly one probe invocation
    try:
        catalogo = preflight.verify(slate_ids=[args.model], table_models=tabla.models)
    except preflight.PreflightError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    _emit(_preflight_line(catalogo))
    try:
        resumen = concurrency.run_probe(
            _base(args),
            model=args.model,
            k_max=args.k_max,
            settle_s=args.settle_s,
            ancla=args.ancla,
            table_version=tabla.table_version,
            catalog={"http": catalogo.http, "ids": catalogo.ids, "matched": catalogo.matched},
            model_map={s: c for s, c in catalogo.matched.items() if c != s},
            emit=_emit,
        )
    except RunnerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(resumen, ensure_ascii=False, indent=2))
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
    if args.settle_s < 0 or not math.isfinite(args.settle_s):
        print(
            f"error: --settle-s must be a finite number >= 0; got {args.settle_s!r}",
            file=sys.stderr,
        )
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
    except TableError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except gate.GateClosed as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not _require_api_key():
        return 2

    def _emit(msg: str) -> None:
        print(msg, flush=True, file=sys.stderr)  # progress is log noise: stdout stays parseable

    gate.consume(_base(args), "T2")  # one dry-run enables exactly one calibration
    try:
        catalogo = preflight.verify(slate_ids=modelos, table_models=tabla.models)
    except preflight.PreflightError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    _emit(_preflight_line(catalogo))
    try:
        resumen = calibration.run_calibration(
            _base(args),
            models=modelos,
            spaced_ages=edades,
            settle_s=args.settle_s,
            table_version=tabla.table_version,
            tabla=tabla,
            catalog={"http": catalogo.http, "ids": catalogo.ids, "matched": catalogo.matched},
            model_map={s: c for s, c in catalogo.matched.items() if c != s},
            emit=_emit,
        )
    except RunnerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(resumen, ensure_ascii=False, indent=2))
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


def cmd_analyze(args: argparse.Namespace) -> int:
    """The re-run without re-measuring: the whole bundle from raw alone.

    The API key is never needed here and never read: analyze works offline on
    the immutable datasets, so a price change (or a new anchor or S1 guess)
    re-derives every derived number with zero quota spent.
    """
    if math.isnan(args.s) or not (0.0 <= args.s <= 1.0):
        print(f"error: --s must be in [0, 1] (S1 cache hit-rate); got {args.s!r}", file=sys.stderr)
        return 2
    if not math.isfinite(args.ancla) or args.ancla <= 0:
        print(f"error: --ancla must be a finite number > 0; got {args.ancla!r}", file=sys.stderr)
        return 2
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
        )
    except analyze.AnalyzeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    carpeta = analyze.write_bundle(
        _base(args), doc, emit=lambda m: print(m, file=sys.stderr, flush=True)
    )
    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0
    bp = doc["base_params"]
    print(
        f"analysis: table={bp['table_version']} ancla={bp['ancla']:g} "
        f"({bp['usd_per_pp']:.6f} USD/pp) s={bp['s']} | raw: "
        f"{doc['raw']['request_lines']} requests, {doc['raw']['batch_lines']} batches"
    )
    conteo = {"legacy": 0, "new": 0, "tie": 0, "no data": 0}
    for c in doc["cells"]:
        conteo[c["verdict"]["s0"]] += 1
    print(
        f"  cells: {len(doc['cells'])} | s0 verdicts: {conteo['legacy']} legacy, "
        f"{conteo['new']} new, {conteo['tie']} tie, {conteo['no data']} no data"
    )
    if doc["paper_discounts"]:
        print("  unmaterialized paper discounts: " + ", ".join(doc["paper_discounts"]))
    print(f"  bundle: {carpeta} (analysis.json, dashboard.html, pngs/)")
    return 0


DESPACHO = {
    "dry-run": cmd_dry_run,
    "run": cmd_run,
    "probe-concurrency": cmd_probe_concurrency,
    "calibrate-cache": cmd_calibrate_cache,
    "analyze": cmd_analyze,
    "status": cmd_status,
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
        else:
            parser.add_argument("--level", choices=["T1", "T2", "T3"], default=None)
        parser.add_argument("--model", default=None)
        parser.add_argument(
            "--pricing-dir", default="pricing", help="tables directory (relative to --base)"
        )
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
            # divide by) and --s (the S1 assumption it extrapolates with).
            # No --reps/--rep/--k: those tune SPENDING, and a silent no-op
            # here would read as a re-measured density instead of a re-priced
            # bundle.
            parser.add_argument("--ancla", type=float, default=100.0, help="P_LEGADO USD/month")
            parser.add_argument("--s", type=float, default=0.5, help="S1 cache hit-rate (0..1)")
        else:
            parser.add_argument("--s", type=float, default=0.5, help="S1 cache hit-rate (0..1)")
            parser.add_argument("--reps", type=int, default=5)
            parser.add_argument("--rep", type=int, default=None, help="run only this repetition")
            parser.add_argument("--k", type=int, default=1, help="concurrency of the burst")
        parser.add_argument(
            "--settle-s", type=float, default=90.0, help="settle between read and read (s)"
        )
        parser.add_argument("--json", action="store_true")
        parser.set_defaults(func=DESPACHO.get(nombre, _stub(nombre)))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
