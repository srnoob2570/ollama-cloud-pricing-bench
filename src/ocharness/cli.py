"""`bench` — the harness CLI. The system's single external seam."""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import pathlib
import sys

from . import cost, gate, workloads
from .pricing import PriceTable, TableError
from .runner import RunnerError, run_level

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
    gate.consume(_base(args), args.level)  # one dry-run enables exactly one run
    modelos = [args.model] if args.model else workloads.slate(args.level, tabla)
    rep_filter = args.rep if args.rep else None

    def _emit(msg: str) -> None:
        print(msg, flush=True, file=sys.stderr)  # progress is log noise: stdout stays parseable

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


DESPACHO = {"dry-run": cmd_dry_run, "run": cmd_run}


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
