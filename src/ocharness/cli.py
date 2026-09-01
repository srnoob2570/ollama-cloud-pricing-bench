"""`bench` — CLI del harness. Único seam externo del sistema."""

from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import sys

from . import cost, gate
from .pricing import ErrorTabla, TablaPrecios

SUBCOMANDOS = ("dry-run", "run", "probe-concurrency", "calibrate-cache", "analyze", "status",
               "resume")


def _base(args: argparse.Namespace) -> pathlib.Path:
    return pathlib.Path(args.base).resolve()


def _pricing_dir(args: argparse.Namespace) -> pathlib.Path:
    """`--pricing-dir` se resuelve contra `--base` cuando es relativo."""
    ruta = pathlib.Path(args.pricing_dir)
    return ruta if ruta.is_absolute() else _base(args) / ruta


def _validar_escenario(args: argparse.Namespace) -> str | None:
    """Valida --s/--reps; devuelve el mensaje de error o None."""
    if not (0.0 <= args.s <= 1.0):
        return f"--s debe estar en [0, 1] (hit-rate del escenario S1); recibí {args.s!r}"
    if args.reps < 1:
        return f"--reps debe ser ≥ 1; recibí {args.reps!r}"
    return None


def cmd_dry_run(args: argparse.Namespace) -> int:
    if args.level is None:
        print("error: dry-run requiere --level", file=sys.stderr)
        return 2
    error = _validar_escenario(args)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    try:
        tabla = TablaPrecios.carga(_pricing_dir(args), args.table_version)
        filas = cost.presupuesto(args.level, tabla, reps=args.reps, s=args.s)
    except (ErrorTabla, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    estimado = {"table_version": tabla.table_version, "nivel": args.level,
                "reps": args.reps, "s": args.s, "filas": [dataclasses.asdict(f) for f in filas]}
    if args.json:
        print(json.dumps(estimado, ensure_ascii=False, indent=2))
    else:
        print(f"table_version={tabla.table_version} nivel={args.level} hit-rate S1={args.s}")
        print(f"{'workload':<20}{'modelos':>8}{'reps':>6}{'requests':>10}"
              f"{'tok_in':>11}{'tok_out':>9}{'$ S0':>9}{'$ S1':>9}{'pp':>9}")
        for f in filas:
            pp = "s/calib" if f.pp_esperado is None else f"{f.pp_esperado:.4f}"
            print(f"{f.workload:<20}{f.modelos:>8}{f.reps:>8}{f.tokens_in:>10,}"
                  f"{f.tokens_out:>9,}{f.costo_s0:>10.4f}{f.costo_s1:>9.4f}{pp:>9}")
        print(f"{'TOTAL ' + args.level:<20}{'':>8}{'':>8}"
              f"{sum(f.tokens_in for f in filas):>10,}{sum(f.tokens_out for f in filas):>9,}"
              f"{sum(f.costo_s0 for f in filas):>10.4f}{sum(f.costo_s1 for f in filas):>9.4f}")
    gate.marcar_dry_run(_base(args), args.level, estimado)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if args.level is None:
        print("error: run requiere --level", file=sys.stderr)
        return 2
    try:
        tabla = TablaPrecios.carga(_pricing_dir(args), args.table_version)
        gate.exigir_dry_run(_base(args), args.level, table_version=tabla.table_version)
    except ErrorTabla as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except gate.CompuertaCerrada as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    gate.consumir(_base(args), args.level)
    print("`run` no implementado todavía (ticket Harness 02)", file=sys.stderr)
    return 3


def _stub(nombre: str):
    def _cmd(args: argparse.Namespace) -> int:
        print(f"`bench {nombre}` aún no está implementado", file=sys.stderr)
        return 3
    return _cmd


DESPACHO = {"dry-run": cmd_dry_run, "run": cmd_run}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bench",
                                description="Harness de benchmarks de costo de Ollama Cloud")
    p.add_argument("--base", default=".", help="directorio de trabajo (pricing/, runs/)")
    sub = p.add_subparsers(dest="comando", required=True)
    for nombre in SUBCOMANDOS:
        parser = sub.add_parser(nombre)
        parser.add_argument("--level", choices=["T1", "T2", "T3"], default=None)
        parser.add_argument("--model", default=None)
        parser.add_argument("--pricing-dir", default="pricing",
                            help="dir de tablas (relativo a --base)")
        parser.add_argument("--table-version", default=None)
        parser.add_argument("--s", type=float, default=0.5, help="hit-rate del escenario S1 (0..1)")
        parser.add_argument("--ancla", type=float, default=100.0, help="P_LEGADO USD/mes")
        parser.add_argument("--reps", type=int, default=5)
        parser.add_argument("--json", action="store_true")
        parser.set_defaults(func=DESPACHO.get(nombre, _stub(nombre)))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())