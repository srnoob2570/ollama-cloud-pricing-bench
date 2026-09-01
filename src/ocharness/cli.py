"""`bench` — the harness CLI. The system's single external seam."""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import pathlib
import sys

from . import cost, gate, preflight, workloads
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
    if args.level != "T1":
        print(
            f"error: run is only implemented for T1 (ticket Harness 02); {args.level} arrives "
            "with a later ticket",
            file=sys.stderr,
        )
        return 3
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

    # Preflight before consuming the mark: a catalog drift aborts with a diff and
    # the dry-run approval stays valid — an aborted preflight is not a run.
    try:
        catalogo = preflight.verify(
            slate_ids=workloads.slate(args.level, tabla), table_models=tabla.models
        )
    except preflight.PreflightError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    _emit(_preflight_line(catalogo))
    gate.consume(_base(args), args.level)  # one dry-run enables exactly one run

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
            catalog={"http": catalogo.http, "ids": catalogo.ids},
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


def _status_doc(nivel: str, manifiesto: Manifest) -> dict:
    """The status of one level's run, computed from its manifest (no API)."""
    doc = manifiesto.doc
    counts: dict[str, int] = {"done": 0, "aborted": 0, "in_flight": 0}
    dpp_session = dpp_weekly = 0.0
    con_bracket = cerrados = 0
    requests_ok = 0
    batches = []
    for batch_id, entrada in doc.get("batches", {}).items():
        estado = entrada.get("status", "?")
        counts[estado] = counts.get(estado, 0) + 1
        dpp_s = entrada.get("dpp_session")
        dpp_w = entrada.get("dpp_weekly")
        if estado in ("done", "aborted"):
            cerrados += 1
        if isinstance(dpp_s, (int, float)) and isinstance(dpp_w, (int, float)):
            dpp_session += dpp_s
            dpp_weekly += dpp_w
            con_bracket += 1
        ok = entrada.get("requests_ok")
        if isinstance(ok, int):
            requests_ok += ok
        batches.append(
            {
                "batch_id": batch_id,
                "status": estado,
                "workload": entrada.get("workload"),
                "model": entrada.get("model"),
                "rep": entrada.get("rep"),
                "dpp_session": dpp_s,
                "dpp_weekly": dpp_w,
                "requests_ok": ok,
                "note": entrada.get("note"),
            }
        )
    planned = int(doc.get("planned", len(batches)))
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
    if counts["aborted"] or counts["in_flight"]:
        print(f"  attention: {counts['aborted']} aborted, {counts['in_flight']} in_flight")
        for b in doc["batches"]:
            if b["status"] in ("aborted", "in_flight"):
                coordenada = f"{b['workload']}/{b['model']}"
                if b.get("rep"):
                    coordenada += f" rep{b['rep']}"
                print(f"    {b['status']}: {coordenada} [{b['batch_id'][:12]}]")


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
        print("no run manifests: nothing has run yet")
        return 0
    resumen = []
    for nivel in niveles:
        try:
            manifiesto = Manifest.load(runs_dir / f"manifest-{nivel}.json")
        except RunnerError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if manifiesto is None:
            print(f"{nivel}: no run manifest - nothing has run for this level")
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


DESPACHO = {"dry-run": cmd_dry_run, "run": cmd_run, "status": cmd_status}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bench", description="Cost benchmark harness for Ollama Cloud")
    p.add_argument("--base", default=".", help="working directory (pricing/, runs/)")
    sub = p.add_subparsers(dest="comando", required=True)
    for nombre in SUBCOMMANDS:
        parser = sub.add_parser(nombre)
        parser.add_argument("--level", choices=["T1", "T2", "T3"], default=None)
        parser.add_argument("--model", default=None)
        parser.add_argument(
            "--pricing-dir", default="pricing", help="tables directory (relative to --base)"
        )
        parser.add_argument("--table-version", default=None)
        parser.add_argument("--s", type=float, default=0.5, help="S1 cache hit-rate (0..1)")
        parser.add_argument("--ancla", type=float, default=100.0, help="P_LEGADO USD/month")
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
