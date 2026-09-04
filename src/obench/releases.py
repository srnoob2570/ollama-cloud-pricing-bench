"""Dataset sync to GitHub releases (methodology v1 §4: "dataset on GitHub releases").

One release per run, pairing raw<->code<->table so a dataset stays interpretable
after prices change:

- `bench release --run <run_id>` packages the run's raw evidence (its
  requests/batches JSONL, the manifest that binds them, the workstream evidence
  when present, and the price-table snapshot that priced them) plus a readable
  copy of the same evidence — `dataset/dataset.json`, one CSV per table and
  `dataset/dataset.xlsx`, flattened by `dataset_export` and stamped into the
  sha256 map like any other packaged file — and a `metadata.json` stamp —
  kind, run, level, models, table_version + its sha256, protocol_version,
  fixture_version, the producing git commit, and a sha256 map over every
  packaged file — into `releases/dataset-<run_id>.tar.gz` and publishes it
  with `gh` under the short canonical asset names (`dataset.tar.gz`,
  `metadata.json`, `notes.md`). A release is never rewritten: publishing over
  an existing tag refuses (raw JSONL is immutable; so is its release), and an
  unfinished run (batches still in_flight) or an unreadable/mismatched table
  snapshot refuses before anything is published — a burnt tag cannot be fixed.
- `bench analyze --release <tag>` fetches the release back with `gh`,
  re-verifies the fetched tree against the metadata's sha256 map in BOTH
  directions (every stamped file matches, and the tarball carries nothing the
  metadata does not stamp — a release edited after publication never enters
  the analysis), and runs the normal offline analysis over the fetched tree
  with the release's OWN table — the pairing, made real.

The credential guardrail is enforced at packaging time: the live
`OLLAMA_API_KEY` (in raw or JSON-escaped form, plus any bearer-token-shaped
string) must not appear in any packaged byte, or the release refuses. Nothing
here ever touches the ollama API or reads the key for anything but the scrub.

The `gh` CLI is the transport: called as a subprocess so the test seam stays a
fake `gh` executable on PATH (the same one-seam philosophy as the fake
ollama.com).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import tarfile
import time

from . import dataset_export
from .pricing import PriceTable, TableError

DATASET_KIND = "obench-dataset"
# the stamp the pre-rename harness shipped: frozen published releases keep it,
# so the validator must keep accepting it (integrity rides the sha256 checks,
# not the kind string)
DATASET_KIND_LEGACY = "ocharness-dataset"
RELEASES_DIR = "releases"
TAG_PREFIX = "run-"
GH_TIMEOUT_S = 600.0  # asset uploads can be slow
GIT_TIMEOUT_S = 30.0

# Credential shapes: the live key is the primary check (its value must not
# appear anywhere); the shape catches a key that was rotated out of the
# environment after the run that leaked it. Case-insensitive: a payload could
# carry it in any case.
_BEARER_SHAPE = re.compile(
    rb"(?:bearer\s+[A-Za-z0-9_\-=+/]{12,}|sk-[A-Za-z0-9]{16,})", re.IGNORECASE
)


class ReleaseError(Exception):
    """The release refused (bad input, dangerous data, or a gh failure)."""


@dataclasses.dataclass(frozen=True)
class Package:
    """What `package()` produced, ready for `publish()`."""

    run_id: str
    tag: str
    tar: pathlib.Path
    metadata: pathlib.Path  # the standalone metadata asset
    notes: pathlib.Path  # the release notes (also the page body)
    doc: dict  # the metadata document itself


def _sha256(ruta: pathlib.Path) -> str:
    return hashlib.sha256(ruta.read_bytes()).hexdigest()


def _count_lines(ruta: pathlib.Path) -> int:
    """Non-blank lines, tolerating a torn multi-byte tail (a crashed writer's
    mark): the release packages bytes as they are, the sha256 map pins them,
    and analyze skips the torn line — refusing here would block the dataset
    forever over one damaged tail."""
    datos = ruta.read_bytes().decode("utf-8", errors="replace")
    return sum(1 for l in datos.splitlines() if l.strip())


def _atomic_write(ruta: pathlib.Path, texto: str) -> None:
    tmp = ruta.with_name(ruta.name + ".tmp")
    tmp.write_text(texto, encoding="utf-8")
    tmp.replace(ruta)


# ---------------------------------------------------------------------------
# the run's files: what a release pairs
# ---------------------------------------------------------------------------


def _find_manifest(runs_dir: pathlib.Path, run_id: str) -> tuple[pathlib.Path, dict] | None:
    """The manifest binding this run_id; a corrupt manifest cannot prove a
    binding, so it is skipped (another one may bind)."""
    for ruta in sorted(runs_dir.glob("manifest-*.json")):
        try:
            doc = json.loads(ruta.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
        if isinstance(doc, dict) and doc.get("run_id") == run_id:
            return ruta, doc
    return None


def _scan_raw(ruta: pathlib.Path) -> tuple[set[str], set[str]]:
    """(table_version stamps, model ids) across the file's parseable lines;
    a torn or damaged line is skipped (the sha256 map pins the bytes either
    way, and analyze is the one that must tolerate torn tails)."""
    versiones: set[str] = set()
    modelos: set[str] = set()
    for linea in ruta.read_bytes().decode("utf-8", errors="replace").splitlines():
        if not linea.strip():
            continue
        try:
            doc = json.loads(linea)
        except json.JSONDecodeError:
            continue
        if not isinstance(doc, dict):
            continue
        if isinstance(doc.get("table_version"), str):
            versiones.add(doc["table_version"])
        if isinstance(doc.get("model"), str):
            modelos.add(doc["model"])
    return versiones, modelos


def collect_run(
    base: pathlib.Path, run_id: str, pricing_dir: pathlib.Path
) -> tuple[list[tuple[str, pathlib.Path]], dict, list[str]]:
    """The run's dataset files with their release paths, its manifest doc, and
    the model ids its raw lines carry.

    A release is never partial and never mispaired: missing raw files, an
    unfinished run (in_flight batches), an unreadable or differently-versioned
    table snapshot, or raw lines stamped with another table all refuse here —
    after publication the tag cannot be rewritten, so every one of these must
    fail BEFORE `gh` is ever called."""
    base = pathlib.Path(base)
    runs_dir = base / "runs"
    batches_dir = base / "batches"
    ruta_requests = runs_dir / f"requests-{run_id}.jsonl"
    ruta_batches = batches_dir / f"batches-{run_id}.jsonl"
    if not (ruta_requests.exists() and ruta_batches.exists()):
        raise ReleaseError(
            f"no dataset for run {run_id!r} under {base} (expected "
            f"runs/requests-{run_id}.jsonl and batches/batches-{run_id}.jsonl)"
        )
    if _count_lines(ruta_requests) == 0 or _count_lines(ruta_batches) == 0:
        raise ReleaseError(
            f"the dataset of run {run_id!r} is empty (no raw lines) - nothing to release"
        )
    hallado = _find_manifest(runs_dir, run_id)
    if hallado is None:
        raise ReleaseError(
            f"no manifest under {runs_dir} binds run {run_id!r} - the release cannot "
            "stamp its level, table and protocol"
        )
    ruta_manifiesto, manifiesto = hallado
    abiertos = sorted(
        {
            str(e.get("status"))
            for e in manifiesto.get("batches", {}).values()
            if not isinstance(e, dict) or e.get("status") not in ("done", "aborted")
        }
    )
    if abiertos:
        # An unfinished run would burn the one-release-per-run tag on partial
        # evidence: aborts are legitimate closed states, in_flight is not.
        raise ReleaseError(
            f"run {run_id!r} is not finished: its manifest holds batches in state "
            f"{', '.join(abiertos)} (`bench status` shows them) - resolve the run "
            "before releasing its dataset"
        )
    tabla_version = manifiesto.get("table_version")
    if not isinstance(tabla_version, str):
        raise ReleaseError(
            f"manifest {ruta_manifiesto.name} does not name its table_version - "
            "the release cannot pair the run with a table"
        )
    ruta_tabla = pathlib.Path(pricing_dir) / f"{tabla_version}.json"
    if not ruta_tabla.exists():
        raise ReleaseError(
            f"the run's table snapshot pricing/{tabla_version}.json is not in the "
            f"pricing directory - the release cannot pair raw<->table"
        )
    try:
        tabla = PriceTable(ruta_tabla)  # parses + validates the rates
    except TableError as e:
        raise ReleaseError(f"the run's table snapshot is unreadable: {e}") from None
    if tabla.table_version != tabla_version:
        raise ReleaseError(
            f"the table snapshot pricing/{ruta_tabla.name} declares table_version "
            f"{tabla.table_version!r} but the run's manifest binds {tabla_version!r} - "
            "a mismatched raw<->table pairing"
        )
    versiones, modelos = _scan_raw(ruta_requests)
    versiones_b, _modelos_b = _scan_raw(ruta_batches)
    versiones |= versiones_b
    divergentes = sorted(v for v in versiones if v != tabla_version)
    if divergentes:
        raise ReleaseError(
            f"the raw lines of run {run_id!r} are stamped with table(s) "
            f"{', '.join(divergentes)} but its manifest binds {tabla_version!r} - the "
            "release would pair the dataset with a table that never priced it"
        )

    archivos: list[tuple[str, pathlib.Path]] = [
        (f"runs/{ruta_requests.name}", ruta_requests),
        (f"batches/{ruta_batches.name}", ruta_batches),
        (f"runs/{ruta_manifiesto.name}", ruta_manifiesto),
        (f"pricing/{ruta_tabla.name}", ruta_tabla),
    ]
    # Workstream evidence rides along when it exists and is readable: the
    # run's own probe volleys always, and every cache-calibration summary whose
    # measured models are among THIS run's models - analyze applies those
    # readings per model no matter which workstream run produced them, so a
    # release without them would analyze with an assumed S1 where the local
    # analysis measured one.
    ruta_probe = runs_dir / f"probe-{run_id}.jsonl"
    if ruta_probe.exists() and _count_lines(ruta_probe) > 0:
        archivos.append((f"runs/{ruta_probe.name}", ruta_probe))
    # The billing canary's raw line rides along when it exists (protocol v3):
    # its ratio and alarm claim must regenerate from shipped raw evidence —
    # nonce hashes, seeds, per-chat outcomes and the four meter payloads —
    # never be taken on faith from the manifest's summary.
    ruta_canary = runs_dir / f"canary-{run_id}.jsonl"
    if ruta_canary.exists() and _count_lines(ruta_canary) > 0:
        archivos.append((f"runs/{ruta_canary.name}", ruta_canary))
    for ruta_cal in sorted(runs_dir.glob("calibration-*.json")):
        try:
            doc = json.loads(ruta_cal.read_text(encoding="utf-8"))
            lecturas = doc["readings"] if isinstance(doc, dict) else None
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, KeyError, TypeError):
            continue  # unreadable evidence: analyze skips it too, so may the release
        if isinstance(lecturas, dict) and any(m in modelos for m in lecturas if isinstance(m, str)):
            archivos.append((f"runs/{ruta_cal.name}", ruta_cal))
    return archivos, manifiesto, sorted(modelos)


def _key_forms(clave: str) -> list[bytes]:
    """The byte forms a JSON dataset could carry the key in: raw, and the two
    JSON escape schemes (the runner writes ensure_ascii=False, which still
    escapes quotes, backslashes and control characters; a hand edit may have
    used ensure_ascii=True)."""
    formas = [clave.encode("utf-8")]
    for ascii_out in (False, True):
        escapada = json.dumps(clave, ensure_ascii=ascii_out)[1:-1].encode("utf-8")
        if escapada not in formas:
            formas.append(escapada)
    return formas


def _scrub(archivos: list[tuple[str, pathlib.Path]]) -> None:
    """The guardrail, enforced at the only moment a release can be stopped:
    the live key (raw or JSON-escaped) and any bearer-shaped string must not
    appear in any byte."""
    clave = os.environ.get("OLLAMA_API_KEY", "")
    formas = _key_forms(clave) if clave else []
    culpables: list[str] = []
    for rel, ruta in archivos:
        blob = ruta.read_bytes()
        if formas and any(f in blob for f in formas):
            culpables.append(f"{rel} (the live OLLAMA_API_KEY)")
        elif _BEARER_SHAPE.search(blob):
            culpables.append(f"{rel} (a bearer-token-shaped string)")
    if culpables:
        raise ReleaseError(
            "credential guard: the dataset carries credential material - "
            + "; ".join(culpables)
            + " - the guardrail forbids credentials in any dataset or release"
        )


# ---------------------------------------------------------------------------
# package
# ---------------------------------------------------------------------------


def _git(argv: list[str], *, cwd: pathlib.Path) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", *argv],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 127, "", ""
    return proc.returncode, proc.stdout, proc.stderr


def git_code(base: pathlib.Path) -> dict:
    """The producing code's identity: its commit and whether the tree was
    dirty. Untracked files never count (the harness's own outputs — runs/,
    batches/, releases/ — are not code changes); only tracked modifications
    and staged work do. Nulls when `base` is not a git checkout (the pairing
    degrades honestly instead of inventing a commit)."""
    code, out, _err = _git(["rev-parse", "HEAD"], cwd=base)
    commit = out.strip() if code == 0 else None
    code_s, out_s, _err = _git(["status", "--porcelain", "--untracked-files=no"], cwd=base)
    dirty = (out_s.strip() != "") if code_s == 0 else None
    return {"git_commit": commit, "dirty": dirty}


def infer_repo(base: pathlib.Path) -> str:
    """owner/name from the git remote `origin` (the owner's usual checkout)."""
    code, out, _err = _git(["remote", "get-url", "origin"], cwd=pathlib.Path(base))
    url = out.strip()
    if code != 0 or not url:
        raise ReleaseError(
            "could not infer the GitHub repo from git remote origin - pass --repo owner/name"
        )
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", url)
    if not m:
        raise ReleaseError(f"cannot parse the GitHub repo from origin ({url!r}) - pass --repo")
    return m.group(1)


def _notes_text(meta: dict) -> str:
    code = meta.get("code") or {}
    commit = code.get("git_commit") or "unknown commit (not a git checkout)"
    sucio = " (the working tree had uncommitted changes)" if code.get("dirty") else ""
    # The v2 freeze (methodology v1.2): every request whose suffix repeats a
    # prefix (bracket reps, k-cells, multi-turn turns) billed at the implicit
    # cache discount, so the v2 dataset undercounts raw work — it is the opacity
    # case study, frozen: never extended, never mixed with protocol v3.
    congelado = (
        "\n- **protocol v2 dataset, frozen**: written before the cache-free lane "
        "(protocol v3) — its suffix-repeating requests bill at the implicit cache "
        "discount, so its legacy pp undercounts raw work. Kept as the opacity case "
        "study; never extended, never mixed with v3 data.\n"
        if meta.get("protocol_version") == "2"
        else ""
    )
    return (
        f"# bench dataset {meta['run_id']}\n\n"
        f"- level: {meta.get('level')} - table_version: {meta['table_version']} - "
        f"protocol_version: {meta['protocol_version']}\n"
        f"- produced by obench at {commit}{sucio}\n"
        f"- {meta['counts']['request_lines']} request lines, "
        f"{meta['counts']['batch_lines']} batch lines{congelado}\n\n"
        "The tarball carries a readable copy of the raw evidence: "
        "`dataset/dataset.json`, one CSV per table (`dataset/requests.csv`, "
        "`dataset/batches.csv`, ...) and `dataset/dataset.xlsx` (a sheet per "
        "table plus a README).\n\n"
        "Consume offline (raw JSONL is immutable; derivatives regenerate from it):\n\n"
        f"    bench analyze --release {TAG_PREFIX}{meta['run_id']} --repo <owner/name>\n"
    )


def package(base, *, run_id: str, pricing_dir) -> Package:
    """One run's dataset -> releases/dataset-<run_id>.tar.gz + metadata asset
    + notes. Raises ReleaseError on an unusable run or a credential hit."""
    base = pathlib.Path(base)
    archivos, manifiesto, modelos = collect_run(base, run_id, pathlib.Path(pricing_dir))
    releases_dir = base / RELEASES_DIR
    releases_dir.mkdir(parents=True, exist_ok=True)
    # The readable copy of the same evidence, derived BEFORE the scrub: a
    # flattened dataset is the raw's own bytes in another shape, so it must
    # clear the same credential guardrail and ship sealed in the sha256 map.
    encabezado = {
        "run_id": run_id,
        "level": manifiesto.get("level"),
        "models": modelos,
        "protocol_version": manifiesto.get("protocol_version"),
        "table_version": manifiesto.get("table_version"),
    }
    try:
        archivos += dataset_export.export_dataset(
            releases_dir / f"dataset-{run_id}", archivos, header=encabezado
        )
    except dataset_export.ExportError as e:
        raise ReleaseError(f"the readable dataset refused: {e}") from None
    _scrub(archivos)
    por_archivo = dict(archivos)
    meta = {
        "kind": DATASET_KIND,
        "run_id": run_id,
        "level": manifiesto.get("level"),
        "models": modelos,
        "protocol_version": manifiesto.get("protocol_version"),
        "fixture_version": manifiesto.get("fixture_version"),
        "table_version": manifiesto.get("table_version"),
        "table_sha256": _sha256(por_archivo[f"pricing/{manifiesto['table_version']}.json"]),
        "code": git_code(base),
        "created_at": time.time(),
        "counts": {
            "request_lines": _count_lines(por_archivo[f"runs/requests-{run_id}.jsonl"]),
            "batch_lines": _count_lines(por_archivo[f"batches/batches-{run_id}.jsonl"]),
        },
        "files": {rel: _sha256(ruta) for rel, ruta in archivos},
    }
    releases_dir = base / RELEASES_DIR
    ruta_meta = releases_dir / f"metadata-{run_id}.json"
    ruta_notes = releases_dir / f"notes-{run_id}.md"
    ruta_tar = releases_dir / f"dataset-{run_id}.tar.gz"
    _atomic_write(ruta_meta, json.dumps(meta, ensure_ascii=False, indent=2))
    _atomic_write(ruta_notes, _notes_text(meta))
    tmp = ruta_tar.with_name(ruta_tar.name + ".tmp")
    with tarfile.open(tmp, "w:gz") as tar:
        for rel, ruta in archivos:
            tar.add(ruta, arcname=rel)
        tar.add(ruta_meta, arcname="metadata.json")  # the tarball alone suffices
    tmp.replace(ruta_tar)
    return Package(
        run_id=run_id,
        tag=f"{TAG_PREFIX}{run_id}",
        tar=ruta_tar,
        metadata=ruta_meta,
        notes=ruta_notes,
        doc=meta,
    )


# ---------------------------------------------------------------------------
# publish + fetch: the gh transport
# ---------------------------------------------------------------------------


def _gh(argv: list[str], *, cwd: pathlib.Path) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["gh", *argv],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GH_TIMEOUT_S,
        )
    except FileNotFoundError:
        raise ReleaseError(
            "the gh CLI is not installed or not on PATH - dataset releases need it "
            "(https://cli.github.com)"
        ) from None
    except subprocess.TimeoutExpired:
        raise ReleaseError(f"gh {argv[0]} {argv[1]} timed out after {GH_TIMEOUT_S:g}s") from None
    return proc.returncode, proc.stdout, proc.stderr


def _commit_is_pushed(commit: str, *, cwd: pathlib.Path) -> bool:
    """Whether some remote branch contains the commit. gh's --target needs the
    commit to exist on the REMOTE: passing a local-only commit fails the whole
    release with an opaque 422, so the tag then simply points at the default
    branch and the metadata keeps the true pairing."""
    code, out, _err = _git(["branch", "-r", "--contains", commit], cwd=cwd)
    return code == 0 and out.strip() != ""


def publish(base, paquete: Package, *, repo: str) -> None:
    """Creates the GitHub release; refuses over an existing tag (a dataset
    release is never rewritten)."""
    base = pathlib.Path(base)
    code, _out, err = _gh(["release", "view", paquete.tag, "-R", repo], cwd=base)
    if code == 0:
        raise ReleaseError(
            f"release {paquete.tag} already exists in {repo} - a dataset release is "
            "never rewritten (raw JSONL is immutable); delete it first only as an "
            "explicit operator decision"
        )
    argv = ["release", "create", paquete.tag]
    # gh uploads with the file's basename, so the short canonical asset names
    # are staged (releases/.stage-<tag>/): dataset.tar.gz, metadata.json,
    # notes.md. The local artefacts keep their run_id suffixes — two runs must
    # never overwrite each other's staged copies.
    escenario = base / RELEASES_DIR / f".stage-{paquete.tag}"
    if escenario.exists():
        shutil.rmtree(escenario)
    escenario.mkdir(parents=True)
    cortos = {
        paquete.tar: "dataset.tar.gz",
        paquete.metadata: "metadata.json",
        paquete.notes: "notes.md",
    }
    try:
        for origen, corto in cortos.items():
            if origen.exists():
                shutil.copy(origen, escenario / corto)
        argv += [str(escenario / corto) for corto in cortos.values()]
        argv += [
            "-R",
            repo,
            "--title",
            f"bench dataset {paquete.run_id}",
            "--notes-file",
            str(escenario / "notes.md"),
        ]
        commit = (paquete.doc.get("code") or {}).get("git_commit")
        if commit and _commit_is_pushed(commit, cwd=base):
            argv += ["--target", commit]  # the tag anchors to the producing code
        code, _out, err = _gh(argv, cwd=base)
    finally:
        shutil.rmtree(escenario, ignore_errors=True)
    if code != 0:
        raise ReleaseError(f"gh release create failed ({code}): {err.strip()}")


def load_metadata(raiz) -> dict:
    """The fetched dataset's stamp, validated (kind + the pairing's keys)."""
    ruta = pathlib.Path(raiz) / "metadata.json"
    if not ruta.exists():
        raise ReleaseError(f"no metadata.json under {ruta.parent} - not an obench dataset")
    try:
        texto = ruta.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise ReleaseError(f"metadata.json is unreadable ({type(e).__name__}: {e})") from None
    try:
        meta = json.loads(texto)
    except json.JSONDecodeError as e:
        raise ReleaseError(f"metadata.json is not valid JSON: {e}") from None
    if not isinstance(meta, dict) or meta.get("kind") not in (DATASET_KIND, DATASET_KIND_LEGACY):
        raise ReleaseError("metadata.json is not an obench dataset stamp (kind mismatch)")
    faltantes = [
        k for k in ("run_id", "table_version", "protocol_version", "files") if k not in meta
    ]
    if faltantes:
        raise ReleaseError(f"metadata.json is missing: {', '.join(faltantes)}")
    return meta


def _verify_files(raiz: pathlib.Path, meta: dict) -> None:
    """Both directions of integrity: every stamped file exists with its exact
    sha256, AND the tree holds nothing the metadata does not stamp (analyze
    globs the tree, so an added file would enter the analysis unverified)."""
    hashes = meta.get("files")
    if not isinstance(hashes, dict) or not hashes:
        raise ReleaseError("metadata.json carries no file hashes - integrity cannot be verified")
    for rel, sha in hashes.items():
        ruta = raiz / rel
        if not ruta.exists():
            raise ReleaseError(f"integrity check failed: {rel} is missing from the dataset")
        real = _sha256(ruta)
        if real != sha:
            raise ReleaseError(
                f"integrity check failed: {rel} does not match its sha256 "
                f"(expected {sha[:12]}..., got {real[:12]}...) - the release was "
                "edited after publication"
            )
    presentes = {p.relative_to(raiz).as_posix() for p in raiz.rglob("*") if p.is_file()}
    extra = presentes - set(hashes) - {"metadata.json"}
    if extra:
        raise ReleaseError(
            "integrity check failed: the tarball carries files the metadata does not "
            f"stamp ({', '.join(sorted(extra))}) - the release was edited after publication"
        )


# A release tag names a run_id: one level, a timestamp and a hex suffix. The
# shape keeps fetch()'s staging and destination paths inside releases/ (a
# slash-bearing tag is legal on GitHub but would crash the paths; ".." would
# walk out of --base).
_TAG_SHAPE = re.compile(r"^run-[A-Za-z0-9][A-Za-z0-9._-]*$")


def fetch(
    base,
    *,
    tag: str,
    repo: str,
    table_version: str | None = None,
    level: str | None = None,
    model: str | None = None,
) -> tuple[pathlib.Path, dict]:
    """Downloads + verifies the release's dataset into `releases/<tag>/` and
    returns (tree, metadata). The tree is a mini-base: runs/, batches/,
    pricing/, metadata.json. Every refusal fires BEFORE the previous fetch is
    replaced, so a refused re-fetch (a mistyped --table-version, say) leaves
    the earlier fetch — and its analysis bundle — untouched."""
    if not _TAG_SHAPE.match(tag):
        raise ReleaseError(
            f"release tag {tag!r} is not a bench dataset tag ({TAG_PREFIX}<run_id>) - "
            "nothing was fetched"
        )
    base = pathlib.Path(base)
    releases_dir = base / RELEASES_DIR
    releases_dir.mkdir(parents=True, exist_ok=True)
    trabajo = releases_dir / f".fetch-{tag}"
    if trabajo.exists():
        shutil.rmtree(trabajo)
    trabajo.mkdir()
    code, _out, err = _gh(["release", "download", tag, "-R", repo, "--dir", str(trabajo)], cwd=base)
    if code != 0:
        raise ReleaseError(f"gh release download {tag} failed ({code}): {err.strip()}")
    # dataset.tar.gz (short canonical name) or dataset-<run>.tar.gz (published).
    tarballs = sorted(trabajo.glob("dataset*.tar.gz"))
    if len(tarballs) != 1:
        raise ReleaseError(
            f"release {tag} does not carry exactly one dataset tarball ({len(tarballs)} found)"
        )
    extraido = trabajo / "dataset"
    extraido.mkdir()
    try:
        with tarfile.open(tarballs[0]) as tar:
            tar.extractall(extraido, filter="data")
    except (tarfile.TarError, EOFError, OSError) as e:
        raise ReleaseError(
            f"the dataset tarball of {tag} is corrupt ({type(e).__name__}: {e}) - integrity refused"
        ) from None
    meta = load_metadata(extraido)
    esperado = f"{TAG_PREFIX}{meta['run_id']}"
    if tag != esperado:
        raise ReleaseError(
            f"release {tag} carries the metadata of run {meta['run_id']!r} (expected "
            f"tag {esperado!r}) - a mismatched raw<->metadata pairing"
        )
    _verify_files(extraido, meta)
    if table_version is not None and meta["table_version"] != table_version:
        raise ReleaseError(
            f"--table-version {table_version!r} does not match the release's table "
            f"{meta['table_version']!r} - a release pairs its dataset with its own table"
        )
    if level is not None and meta.get("level") != level:
        raise ReleaseError(
            f"the release covers level {meta.get('level')!r}, not {level!r} - it holds "
            "nothing of that level"
        )
    if model is not None and model not in meta.get("models", []):
        raise ReleaseError(
            f"the release's models do not include {model!r} "
            f"({', '.join(meta.get('models', [])) or 'none recorded'})"
        )
    destino = releases_dir / tag
    preservados: list[tuple[str, pathlib.Path]] = []
    if destino.exists():
        # The analysis bundles under the fetched tree are derived AFTER the
        # fetch (`bench analyze --release <tag>` writes analysis/ and the
        # analysis-s<x>/ stamped sets there): a re-fetch refreshes the raw
        # dataset, never the derived bundles - the persisted reference and
        # every stamped set survive the re-download (methodology v1.2, #46).
        for hijo in sorted(destino.iterdir()):
            if hijo.is_dir() and hijo.name.startswith("analysis"):
                resguardado = trabajo / "_preserve" / hijo.name
                resguardado.parent.mkdir(exist_ok=True)
                shutil.move(str(hijo), str(resguardado))
                preservados.append((hijo.name, resguardado))
        shutil.rmtree(destino)
    extraido.rename(destino)
    for nombre, resguardado in preservados:
        shutil.move(str(resguardado), str(destino / nombre))
    shutil.rmtree(trabajo)
    return destino, meta


def release_table(stage) -> PriceTable:
    """The fetched release's own price table — the pairing's `table` half,
    cross-checked against the metadata's stamp."""
    meta = load_metadata(stage)
    ruta = pathlib.Path(stage) / "pricing" / f"{meta['table_version']}.json"
    if not ruta.exists():
        raise ReleaseError(
            f"the release's table snapshot pricing/{meta['table_version']}.json is "
            "missing from the fetched dataset"
        )
    tabla = PriceTable(ruta)
    if tabla.table_version != meta["table_version"]:
        raise ReleaseError(
            f"the release's table snapshot declares table_version {tabla.table_version!r} "
            f"but its metadata stamps {meta['table_version']!r} - a mismatched pairing"
        )
    return tabla
