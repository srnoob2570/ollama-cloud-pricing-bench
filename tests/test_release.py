"""Dataset sync to GitHub releases: ticket Harness 10.

`bench release --run <id>` packages one run's raw dataset (requests + batches +
manifest + the pricing snapshot) with its metadata (code, table, protocol) into
a release; `bench analyze --release <tag>` consumes it back offline. The seam
stays the CLI: a fake `gh` executable on PATH records every invocation and
stores the assets it was given, so every assertion below lands on produced
artifacts and on the calls the fake observed - the real network is never hit.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import tarfile

import pytest
from conftest import write_table
from test_dry_run import run_cli

from ocharness.client import PROTOCOL_VERSION
from ocharness.schema import validate_batch_line, validate_request_line

REPO = "acme/datasets"

# The two-model table the hand-crafted datasets are priced with (cache discount
# on alpha, cached=input on beta - the S0/S1 pair needs both shapes).
TABLE = {
    "alpha": {"input": 0.60, "cached_input": 0.30, "output": 1.20},
    "beta": {"input": 1.00, "cached_input": 1.00, "output": 2.00},
}

# A fake gh: only the three invocations the harness makes, recording argv and
# storing each release's assets (plus its flags) under the state dir.
FAKE_GH = """\
#!/usr/bin/env python3
import json, os, pathlib, shutil, sys

state = pathlib.Path(os.environ["OCHARNESS_FAKE_GH_STATE"])
argv = sys.argv[1:]
with (state / "calls.jsonl").open("a", encoding="utf-8") as f:
    print(json.dumps(argv), file=f)

def repo():
    return argv[argv.index("-R") + 1] if "-R" in argv else "unknown/unknown"

def store(tag):
    return state / "releases" / repo() / tag

def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)

if argv[:2] == ["release", "view"]:
    if store(argv[2]).is_dir():
        print("title")
        sys.exit(0)
    die("release not found")

if argv[:2] == ["release", "create"]:
    tag, rest = argv[2], argv[3:]
    files, flags = [], {}
    i = 0
    while i < len(rest):
        if rest[i] in ("--title", "--notes-file", "--target"):
            flags[rest[i]] = rest[i + 1]
            i += 2
        elif rest[i] == "-R":
            i += 2
        else:
            files.append(rest[i])
            i += 1
    destino = store(tag)
    if destino.is_dir():
        die("already exists")
    destino.mkdir(parents=True)
    notes = (
        pathlib.Path(flags["--notes-file"]).read_text(encoding="utf-8")
        if "--notes-file" in flags
        else ""
    )
    (destino / "_notes.md").write_text(notes, encoding="utf-8")
    (destino / "_flags.json").write_text(json.dumps(flags), encoding="utf-8")
    for ruta in files:
        shutil.copy(ruta, destino / pathlib.Path(ruta).name)
    sys.exit(0)

if argv[:2] == ["release", "download"]:
    src = store(argv[2])
    if not src.is_dir():
        die("release not found")
    d = pathlib.Path(argv[argv.index("--dir") + 1])
    d.mkdir(parents=True, exist_ok=True)
    for p in sorted(src.iterdir()):
        if not p.name.startswith("_"):
            shutil.copy(p, d / p.name)
    sys.exit(0)

die(f"fake gh: unsupported invocation: {argv}")
"""


@pytest.fixture()
def fake_gh(tmp_path, monkeypatch) -> pathlib.Path:
    """The fake gh wired as the release seam: PATH wins over any real gh."""
    estado = tmp_path / "gh-fake"
    estado.mkdir()
    carpeta_bin = tmp_path / "gh-bin"
    carpeta_bin.mkdir()
    gh = carpeta_bin / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setenv("OCHARNESS_FAKE_GH_STATE", str(estado))
    monkeypatch.setenv("PATH", str(carpeta_bin) + os.pathsep + os.environ["PATH"])
    return estado


def gh_calls(estado: pathlib.Path) -> list[list[str]]:
    ruta = estado / "calls.jsonl"
    if not ruta.exists():
        return []
    return [json.loads(l) for l in ruta.read_text(encoding="utf-8").splitlines() if l.strip()]


def gh_assets(estado: pathlib.Path, tag: str) -> pathlib.Path:
    return estado / "releases" / REPO / tag


# ---------------------------------------------------------------------------
# a hand-crafted dataset: one schema-valid run priced by the local table
# ---------------------------------------------------------------------------

RUN = "run-x"
FIX = "fix" * 16


def hand_dataset(tmp_path, run_id: str = RUN, *, poison: str | None = None) -> str:
    """One run's raw dataset + its pricing table. `poison` injects a string
    into the raw evidence (the credential-scrub test's bait)."""
    write_table(tmp_path / "pricing", "2026-08-31", TABLE)
    batch_id = f"{run_id}-batch"
    linea_req = {
        "req_id": f"{batch_id}-0000",
        "batch_id": batch_id,
        "run_id": run_id,
        "level": "T1",
        "workload": "qa_short",
        "model": "alpha",
        "seed": 1,
        "rep": 1,
        "k": 1,
        "t_start": 1.0,
        "t_first_chunk": 1.05,
        "t_total": 2.0,
        "chunks": 3,
        "tok_in": 1000,
        "tok_out": 500,
        "tok_cached": None,
        "api": {"done": True},
        "http": 200,
        "err": poison,
        "checker": "pass",
        "tool_calls": [],
        "steps": [],
        "sandbox": None,
        "out_text_hash": "h" * 64,
        "fixture_hash": FIX,
        "table_version": "2026-08-31",
        "protocol_version": PROTOCOL_VERSION,
    }
    linea_batch = {
        "batch_id": batch_id,
        "run_id": run_id,
        "level": "T1",
        "workload": "qa_short",
        "model": "alpha",
        "fixture_hash": FIX,
        "k": 1,
        "n": 1,
        "settle_s": 90.0,
        "count_check_s": 0.5,
        "wall_clock_s": 1.0,
        "medidor_pre": {"limits": {"session": {"usage": 0.5}, "weekly": {"usage": 0.6}}},
        "medidor_post": {"limits": {"session": {"usage": 0.5}, "weekly": {"usage": 0.602}}},
        "dpp_session": 0.2,
        "dpp_weekly": 0.2,
        "request_counts": {"pre": {}, "count_check": {}, "post": {}},
        "table_version": "2026-08-31",
        "protocol_version": PROTOCOL_VERSION,
        "notes": "",
    }
    validate_request_line(linea_req)
    validate_batch_line(linea_batch)
    manifiesto = {
        "run_id": run_id,
        "level": "T1",
        "table_version": "2026-08-31",
        "protocol_version": PROTOCOL_VERSION,
        "fixture_version": "f1",
        "k": 1,
        "started_at": 1.0,
        "planned": 1,
        "batches": {batch_id: {"status": "done"}},
    }
    (tmp_path / "runs").mkdir(exist_ok=True)
    (tmp_path / "batches").mkdir(exist_ok=True)
    (tmp_path / "runs" / f"requests-{run_id}.jsonl").write_text(
        json.dumps(linea_req) + "\n", encoding="utf-8"
    )
    (tmp_path / "batches" / f"batches-{run_id}.jsonl").write_text(
        json.dumps(linea_batch) + "\n", encoding="utf-8"
    )
    (tmp_path / "runs" / "manifest-T1.json").write_text(
        json.dumps(manifiesto, indent=2), encoding="utf-8"
    )
    return str(tmp_path / "pricing")


# ---------------------------------------------------------------------------
# package + publish
# ---------------------------------------------------------------------------


def test_release_publishes_a_run_end_to_end(tmp_path, fake_cli, fake_gh):
    """The real loop: a T1 run's dataset goes to a release, whole and stamped."""
    from test_run import prepare

    prepare(tmp_path)
    assert run_cli(tmp_path, "run", "--level", "T1", "--settle-s", "0", "--reps", "1")[0] == 0
    run_id = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text())["run_id"]

    codigo, salida, errores = run_cli(tmp_path, "release", "--run", run_id, "--repo", REPO)
    assert codigo == 0, salida or errores
    tag = f"run-{run_id}"
    creates = [c for c in gh_calls(fake_gh) if c[:2] == ["release", "create"]]
    assert len(creates) == 1
    create = creates[0]
    assert create[2] == tag
    assert create[create.index("-R") + 1] == REPO

    activos = sorted(
        p.name for p in gh_assets(fake_gh, tag).iterdir() if not p.name.startswith("_")
    )
    assert activos == [
        f"dataset-{run_id}.tar.gz",
        f"metadata-{run_id}.json",
    ]
    with tarfile.open(gh_assets(fake_gh, tag) / f"dataset-{run_id}.tar.gz") as tar:
        miembros = sorted(tar.getnames())
    assert miembros == sorted(
        [
            f"runs/requests-{run_id}.jsonl",
            f"batches/batches-{run_id}.jsonl",
            "runs/manifest-T1.json",
            "pricing/2026-08-31.json",
            "metadata.json",
        ]
    )
    # The metadata stamps the raw<->code<->table pairing...
    meta = json.loads(
        (gh_assets(fake_gh, tag) / f"metadata-{run_id}.json").read_text(encoding="utf-8")
    )
    assert meta["kind"] == "ocharness-dataset"
    assert meta["run_id"] == run_id and meta["level"] == "T1"
    assert meta["table_version"] == "2026-08-31"
    assert meta["protocol_version"] == PROTOCOL_VERSION
    assert meta["counts"] == {"request_lines": 19 * 24, "batch_lines": 19 * 3}
    # ...and hashes every packaged file (integrity is re-checked on fetch).
    with tarfile.open(gh_assets(fake_gh, tag) / f"dataset-{run_id}.tar.gz") as tar:
        for rel, sha in meta["files"].items():
            assert len(sha) == 64
            contenido = tar.extractfile(rel).read()
            assert hashlib.sha256(contenido).hexdigest() == sha, rel
    notas = (gh_assets(fake_gh, tag) / "_notes.md").read_text(encoding="utf-8")
    assert tag in notas and "analyze --release" in notas


def test_release_priced_table_snapshot_matches_the_run(tmp_path, fake_cli, fake_gh):
    """The release pairs the run with ITS table snapshot: a different table in
    the pricing dir must not leak into the release."""
    from test_run import prepare

    prepare(tmp_path)  # table 2026-08-31
    write_table(tmp_path / "pricing", "2026-09-01", {"alpha": TABLE["alpha"]})  # a newer one
    assert (
        run_cli(
            tmp_path,
            "run",
            "--level",
            "T1",
            "--settle-s",
            "0",
            "--reps",
            "1",
            "--table-version",
            "2026-08-31",
        )[0]
        == 0
    )
    run_id = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text())["run_id"]
    # no --table-version: the manifest binds the run's table; the release pairs it
    assert run_cli(tmp_path, "release", "--run", run_id, "--repo", REPO)[0] == 0
    with tarfile.open(gh_assets(fake_gh, f"run-{run_id}") / f"dataset-{run_id}.tar.gz") as tar:
        assert "pricing/2026-08-31.json" in tar.getnames()
        assert "pricing/2026-09-01.json" not in tar.getnames()


def test_release_refuses_an_unknown_run(tmp_path, fake_gh):
    pricing = hand_dataset(tmp_path)
    codigo, _, errores = run_cli(
        tmp_path, "release", "--run", "no-such-run", "--repo", REPO, "--pricing-dir", pricing
    )
    assert codigo == 2
    assert "no-such-run" in errores
    assert gh_calls(fake_gh) == []  # nothing reached gh


def test_release_refuses_an_empty_dataset(tmp_path, fake_gh):
    pricing = hand_dataset(tmp_path)
    (tmp_path / "runs" / f"requests-{RUN}.jsonl").write_text("", encoding="utf-8")
    codigo, _, errores = run_cli(
        tmp_path, "release", "--run", RUN, "--repo", REPO, "--pricing-dir", pricing
    )
    assert codigo == 2
    assert "empty" in errores
    assert gh_calls(fake_gh) == []


def test_release_refuses_a_run_without_a_manifest(tmp_path, fake_gh):
    pricing = hand_dataset(tmp_path)
    (tmp_path / "runs" / "manifest-T1.json").unlink()
    codigo, _, errores = run_cli(
        tmp_path, "release", "--run", RUN, "--repo", REPO, "--pricing-dir", pricing
    )
    assert codigo == 2
    assert "manifest" in errores
    assert gh_calls(fake_gh) == []


def test_release_refuses_when_the_table_snapshot_is_missing(tmp_path, fake_gh):
    pricing = hand_dataset(tmp_path)
    (tmp_path / "pricing" / "2026-08-31.json").unlink()
    codigo, _, errores = run_cli(
        tmp_path, "release", "--run", RUN, "--repo", REPO, "--pricing-dir", pricing
    )
    assert codigo == 2
    assert "2026-08-31" in errores
    assert gh_calls(fake_gh) == []


def test_release_never_rewrites_a_published_release(tmp_path, fake_gh):
    pricing = hand_dataset(tmp_path)
    assert (
        run_cli(tmp_path, "release", "--run", RUN, "--repo", REPO, "--pricing-dir", pricing)[0] == 0
    )
    codigo, _, errores = run_cli(
        tmp_path, "release", "--run", RUN, "--repo", REPO, "--pricing-dir", pricing
    )
    assert codigo == 2
    assert "already" in errores
    creates = [c for c in gh_calls(fake_gh) if c[:2] == ["release", "create"]]
    assert len(creates) == 1  # the second attempt only viewed


def test_release_scrubs_the_api_key_from_the_dataset(tmp_path, monkeypatch, fake_gh):
    """The guardrail: a credential that reached the raw data blocks the release."""
    clave = "ollama-live-secret-777"
    monkeypatch.setenv("OLLAMA_API_KEY", clave)
    pricing = hand_dataset(tmp_path, poison=clave)
    codigo, _, errores = run_cli(
        tmp_path, "release", "--run", RUN, "--repo", REPO, "--pricing-dir", pricing
    )
    assert codigo == 2
    assert "credential" in errores and f"requests-{RUN}.jsonl" in errores
    assert gh_calls(fake_gh) == []


def test_release_scrubs_bearer_shaped_tokens(tmp_path, monkeypatch, fake_gh):
    """A credential shape (e.g. a rotated-out key the env no longer holds) is
    caught too: a meter payload echoing an Authorization header blocks it."""
    monkeypatch.setenv("OLLAMA_API_KEY", "otra-clave-sin-relacion")
    pricing = hand_dataset(tmp_path, poison="Bearer abcdef0123456789abcdef")
    codigo, _, errores = run_cli(
        tmp_path, "release", "--run", RUN, "--repo", REPO, "--pricing-dir", pricing
    )
    assert codigo == 2
    assert "credential" in errores
    assert gh_calls(fake_gh) == []


def test_release_reports_a_missing_gh_cleanly(tmp_path, monkeypatch, fake_cli):
    """No gh on PATH is a clean refusal, never a traceback."""
    from test_run import prepare

    prepare(tmp_path)
    assert run_cli(tmp_path, "run", "--level", "T1", "--settle-s", "0", "--reps", "1")[0] == 0
    run_id = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text())["run_id"]
    monkeypatch.setenv("PATH", str(tmp_path / "no-gh-here"))
    codigo, _, errores = run_cli(tmp_path, "release", "--run", run_id, "--repo", REPO)
    assert codigo == 2
    assert "gh" in errores.lower()
    assert "Traceback" not in errores


def test_release_carries_the_code_commit(tmp_path, fake_cli, fake_gh):
    """The metadata pairs the dataset with the producing commit; `dirty` means
    uncommitted TRACKED changes (the harness's own outputs are not code); and
    gh gets --target only once the commit exists on the remote (a local-only
    commit would fail the release with an opaque 422)."""
    from test_run import prepare

    from ocharness import releases

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }

    def git(*argv):
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", *argv],
            cwd=tmp_path,
            env=env,
            check=True,
            capture_output=True,
        )

    git("init", "-q", "-b", "main")
    git("commit", "--allow-empty", "-q", "-m", "base")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    git("remote", "add", "origin", str(tmp_path / "remote.git"))  # configured, nothing pushed
    (tmp_path / "nota.txt").write_text("staged, uncommitted\n", encoding="utf-8")
    git("add", "nota.txt")  # a tracked-but-uncommitted change -> dirty

    prepare(tmp_path)
    assert run_cli(tmp_path, "run", "--level", "T1", "--settle-s", "0", "--reps", "1")[0] == 0
    run_id = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text())["run_id"]
    codigo, salida, errores = run_cli(
        tmp_path, "release", "--run", run_id, "--repo", REPO, "--json"
    )
    assert codigo == 0, salida or errores
    doc = json.loads(salida)
    assert doc["metadata"]["code"] == {"git_commit": sha, "dirty": True}
    creates = [c for c in gh_calls(fake_gh) if c[:2] == ["release", "create"]]
    assert len(creates) == 1 and "--target" not in creates[0]  # unpushed: no --target

    # commit + push, then a fresh run releases with the tag anchored to the code
    git("init", "-q", "--bare", str(tmp_path / "remote.git"))
    git("add", "-A")
    git("commit", "-q", "-m", "datasets")
    git("push", "-q", "origin", "main")
    assert releases.git_code(tmp_path)["dirty"] is False  # untracked outputs never count
    assert run_cli(tmp_path, "dry-run", "--level", "T2", "--reps", "1")[0] == 0
    assert (
        run_cli(
            tmp_path,
            "run",
            "--level",
            "T2",
            "--model",
            "glm-5.3-flash",
            "--settle-s",
            "0",
            "--reps",
            "1",
        )[0]
        == 0
    )
    run_id_2 = json.loads((tmp_path / "runs" / "manifest-T2.json").read_text())["run_id"]
    sha_2 = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    codigo, salida, errores = run_cli(
        tmp_path, "release", "--run", run_id_2, "--repo", REPO, "--json"
    )
    assert codigo == 0, salida or errores
    doc2 = json.loads(salida)
    assert doc2["metadata"]["code"] == {"git_commit": sha_2, "dirty": False}
    creates = [c for c in gh_calls(fake_gh) if c[:2] == ["release", "create"]]
    assert "--target" in creates[-1] and sha_2 in creates[-1]


# ---------------------------------------------------------------------------
# consume: analyze --release
# ---------------------------------------------------------------------------


def test_analyze_consumes_the_release_offline(tmp_path, fake_cli, fake_gh):
    """The pairing made real: the same cells from the release as from the local
    dataset, zero API calls, the release's own table pricing them."""
    from test_run import prepare

    prepare(tmp_path)
    assert run_cli(tmp_path, "run", "--level", "T1", "--settle-s", "0", "--reps", "1")[0] == 0
    run_id = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text())["run_id"]
    tag = f"run-{run_id}"
    assert run_cli(tmp_path, "release", "--run", run_id, "--repo", REPO, "--json")[0] == 0

    codigo, salida, errores = run_cli(tmp_path, "analyze", "--json")
    assert codigo == 0, salida or errores
    local = json.loads(salida)

    codigo, salida, errores = run_cli(
        tmp_path, "analyze", "--release", tag, "--repo", REPO, "--json"
    )
    assert codigo == 0, salida or errores
    remoto = json.loads(salida)

    assert remoto["raw"]["run_ids"] == [run_id]
    assert remoto["base_params"]["table_version"] == "2026-08-31"
    assert remoto["cells"] == local["cells"]  # same raw, same table, same params
    assert (tmp_path / "releases" / tag / "analysis" / "analysis.json").exists()
    assert (tmp_path / "releases" / tag / "analysis" / "dashboard.html").exists()
    descargas = [c for c in gh_calls(fake_gh) if c[:2] == ["release", "download"]]
    assert len(descargas) == 1 and tag in descargas[0]


def test_analyze_release_binds_the_table_version(tmp_path, fake_cli, fake_gh):
    from test_run import prepare

    prepare(tmp_path)
    assert run_cli(tmp_path, "run", "--level", "T1", "--settle-s", "0", "--reps", "1")[0] == 0
    run_id = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text())["run_id"]
    assert run_cli(tmp_path, "release", "--run", run_id, "--repo", REPO)[0] == 0
    codigo, _, errores = run_cli(
        tmp_path,
        "analyze",
        "--release",
        f"run-{run_id}",
        "--repo",
        REPO,
        "--table-version",
        "2026-09-01",
    )
    assert codigo == 2
    assert "table" in errores


def test_analyze_release_refuses_a_tampered_release(tmp_path, fake_cli, fake_gh):
    """A release edited after publication never enters the analysis: the
    metadata's sha256 map re-verifies every file on fetch."""
    from test_run import prepare

    prepare(tmp_path)
    assert run_cli(tmp_path, "run", "--level", "T1", "--settle-s", "0", "--reps", "1")[0] == 0
    run_id = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text())["run_id"]
    assert run_cli(tmp_path, "release", "--run", run_id, "--repo", REPO)[0] == 0
    activos = gh_assets(fake_gh, f"run-{run_id}")

    # 1) a well-formed tarball whose CONTENT no longer matches the metadata
    tarball = activos / f"dataset-{run_id}.tar.gz"
    with tarfile.open(tarball) as tar:
        destino = tmp_path / "retar"
        destino.mkdir()
        tar.extractall(destino, filter="data")
    victima = destino / f"runs/requests-{run_id}.jsonl"
    lineas = victima.read_text(encoding="utf-8").splitlines()
    victima.write_text(lineas[0] + "\n", encoding="utf-8")  # one line dropped
    with tarfile.open(tarball, "w:gz") as t:
        for p in sorted(destino.rglob("*")):
            if p.is_file():
                t.add(p, arcname=p.relative_to(destino).as_posix())
    codigo, _, errores = run_cli(tmp_path, "analyze", "--release", f"run-{run_id}", "--repo", REPO)
    assert codigo == 2
    assert "sha256" in errores.lower()

    # 2) a truncated tarball (a partial upload): not even a readable archive
    tarball.write_bytes(tarball.read_bytes()[: len(tarball.read_bytes()) // 2])
    codigo, _, errores = run_cli(tmp_path, "analyze", "--release", f"run-{run_id}", "--repo", REPO)
    assert codigo == 2
    assert "integrity" in errores.lower() or "tarball" in errores.lower()


def test_analyze_release_refuses_a_metadata_of_another_run(tmp_path, fake_cli, fake_gh):
    """A metadata whose run_id does not match the tag is not this tag's dataset."""
    from test_run import prepare

    prepare(tmp_path)
    assert run_cli(tmp_path, "run", "--level", "T1", "--settle-s", "0", "--reps", "1")[0] == 0
    run_id = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text())["run_id"]
    assert run_cli(tmp_path, "release", "--run", run_id, "--repo", REPO)[0] == 0
    activos = gh_assets(fake_gh, f"run-{run_id}")
    meta = json.loads((activos / f"metadata-{run_id}.json").read_text(encoding="utf-8"))
    meta["run_id"] = "run-otro"
    (activos / f"metadata-{run_id}.json").write_text(json.dumps(meta), encoding="utf-8")
    # the metadata INSIDE the tarball must lie too: rebuild it consistently
    with tarfile.open(activos / f"dataset-{run_id}.tar.gz") as tar:
        destino = tmp_path / "retar2"
        destino.mkdir()
        tar.extractall(destino, filter="data")
    (destino / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    with tarfile.open(activos / f"dataset-{run_id}.tar.gz", "w:gz") as t:
        for p in sorted(destino.rglob("*")):
            if p.is_file():
                t.add(p, arcname=p.relative_to(destino).as_posix())
    codigo, _, errores = run_cli(tmp_path, "analyze", "--release", f"run-{run_id}", "--repo", REPO)
    assert codigo == 2
    assert "run-otro" in errores or "tag" in errores


# ---------------------------------------------------------------------------
# resume: the spec's 7th subcommand, realized as run's resumable twin
# ---------------------------------------------------------------------------


def test_resume_skips_completed_batches_without_new_requests(tmp_path, fake_cli):
    from test_run import consumer_calls, prepare

    pricing = prepare(tmp_path)
    assert run_cli(tmp_path, "run", "--level", "T1", "--settle-s", "0", "--reps", "1")[0] == 0
    antes = len(consumer_calls(fake_cli))
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T1", "--reps", "1", "--pricing-dir", pricing)[0]
        == 0
    )
    codigo, salida, errores = run_cli(
        tmp_path, "resume", "--level", "T1", "--settle-s", "0", "--reps", "1"
    )
    assert codigo == 0, salida or errores
    assert "not implemented" not in (salida + errores)
    assert len(consumer_calls(fake_cli)) == antes  # zero new requests: every batch was done


# ---------------------------------------------------------------------------
# the review fixes: pairing guards, integrity in both directions, and parity
# ---------------------------------------------------------------------------


def test_release_refuses_an_unfinished_run(tmp_path, fake_gh):
    """A run with in_flight batches is not a dataset yet: releasing it would
    burn the one-release-per-run tag on partial evidence."""
    pricing = hand_dataset(tmp_path)
    ruta = tmp_path / "runs" / "manifest-T1.json"
    manifiesto = json.loads(ruta.read_text(encoding="utf-8"))
    victima = next(iter(manifiesto["batches"]))
    manifiesto["batches"][victima]["status"] = "in_flight"
    ruta.write_text(json.dumps(manifiesto), encoding="utf-8")
    codigo, _, errores = run_cli(
        tmp_path, "release", "--run", RUN, "--repo", REPO, "--pricing-dir", pricing
    )
    assert codigo == 2
    assert "in_flight" in errores
    assert gh_calls(fake_gh) == []


def test_release_refuses_a_corrupt_table_snapshot(tmp_path, fake_gh):
    """An unreadable table snapshot can never be consumed after publication
    (the tag cannot be rewritten), so packaging refuses."""
    pricing = hand_dataset(tmp_path)
    (tmp_path / "pricing" / "2026-08-31.json").write_text("{not json", encoding="utf-8")
    codigo, _, errores = run_cli(
        tmp_path, "release", "--run", RUN, "--repo", REPO, "--pricing-dir", pricing
    )
    assert codigo == 2
    assert "unreadable" in errores
    assert gh_calls(fake_gh) == []


def test_release_refuses_a_mismatched_raw_table_stamp(tmp_path, fake_gh):
    """Raw lines priced under another table than the manifest binds would be
    published with rates they were never measured under."""
    pricing = hand_dataset(tmp_path)
    ruta = tmp_path / "runs" / f"requests-{RUN}.jsonl"
    linea = json.loads(ruta.read_text(encoding="utf-8"))
    linea["table_version"] = "2026-09-01"
    ruta.write_text(json.dumps(linea) + "\n", encoding="utf-8")
    codigo, _, errores = run_cli(
        tmp_path, "release", "--run", RUN, "--repo", REPO, "--pricing-dir", pricing
    )
    assert codigo == 2
    assert "2026-09-01" in errores
    assert gh_calls(fake_gh) == []


def test_release_carries_the_model_calibrations_and_skips_blank_sidecars(
    tmp_path, fake_cli, fake_gh
):
    """A release pairs the run with the calibration summaries of ITS models
    (analyze applies them per model no matter which workstream run produced
    them) — and a blank sidecar from a crashed writer never blocks it."""
    from test_run import prepare

    prepare(tmp_path)
    assert run_cli(tmp_path, "run", "--level", "T1", "--settle-s", "0", "--reps", "1")[0] == 0
    run_id = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text())["run_id"]
    # a conclusive reading for a model THIS run measured, one for a stranger,
    # and one blank sidecar from a killed writer
    (tmp_path / "runs" / "calibration-T2-cache-x.json").write_text(
        json.dumps({"readings": {"glm-5.3-flash": {"conclusive": True, "hit_rate": 0.9}}}),
        encoding="utf-8",
    )
    (tmp_path / "runs" / "calibration-other.json").write_text(
        json.dumps({"readings": {"zeta": {"conclusive": True, "hit_rate": 0.5}}}),
        encoding="utf-8",
    )
    (tmp_path / "runs" / f"probe-{run_id}.jsonl").write_text("", encoding="utf-8")

    tag = f"run-{run_id}"
    assert run_cli(tmp_path, "release", "--run", run_id, "--repo", REPO)[0] == 0
    with tarfile.open(gh_assets(fake_gh, tag) / f"dataset-{run_id}.tar.gz") as tar:
        nombres = tar.getnames()
    assert "runs/calibration-T2-cache-x.json" in nombres  # the run's model's reading
    assert "runs/calibration-other.json" not in nombres  # nobody in this run is zeta
    assert f"runs/probe-{run_id}.jsonl" not in nombres  # blank sidecar skipped, not fatal

    # and analyze --release prices the model with the MEASURED hit rate, not S1
    codigo, salida, errores = run_cli(
        tmp_path, "analyze", "--release", tag, "--repo", REPO, "--json"
    )
    assert codigo == 0, salida or errores
    remoto = json.loads(salida)
    assert remoto["s_per_model"]["glm-5.3-flash"]["source"] == "measured"
    assert remoto["s_per_model"]["glm-5.3-flash"]["s"] == 0.9


def test_analyze_release_refuses_added_files(tmp_path, fake_cli, fake_gh):
    """analyze globs the whole fetched tree: an ADDED file must not enter the
    analysis unverified (integrity works in both directions)."""
    from test_run import prepare

    prepare(tmp_path)
    assert run_cli(tmp_path, "run", "--level", "T1", "--settle-s", "0", "--reps", "1")[0] == 0
    run_id = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text())["run_id"]
    assert run_cli(tmp_path, "release", "--run", run_id, "--repo", REPO)[0] == 0
    tarball = gh_assets(fake_gh, f"run-{run_id}") / f"dataset-{run_id}.tar.gz"
    with tarfile.open(tarball) as tar:
        destino = tmp_path / "retar"
        destino.mkdir()
        tar.extractall(destino, filter="data")
    # add an extra batch file with one extra line (analyze would fold it in)
    extra = destino / "batches" / f"batches-extra.jsonl"
    extra.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "level": "T1",
                "workload": "qa_short",
                "model": "beta",
                "k": 1,
                "dpp_weekly": 9.9,
                "table_version": "2026-08-31",
            }
        ),
        encoding="utf-8",
    )
    with tarfile.open(tarball, "w:gz") as t:
        for p in sorted(destino.rglob("*")):
            if p.is_file():
                t.add(p, arcname=p.relative_to(destino).as_posix())
    codigo, _, errores = run_cli(tmp_path, "analyze", "--release", f"run-{run_id}", "--repo", REPO)
    assert codigo == 2
    assert "does not stamp" in errores


def test_analyze_release_refuses_a_slash_tag(tmp_path, fake_gh):
    """A slash-bearing tag is legal on GitHub but is not a bench dataset tag:
    refused before anything touches the filesystem or gh."""
    hand_dataset(tmp_path)
    codigo, _, errores = run_cli(tmp_path, "analyze", "--release", "run-2026/v2", "--repo", REPO)
    assert codigo == 2
    assert "not a bench dataset tag" in errores
    assert "Traceback" not in errores
    assert gh_calls(fake_gh) == []


def test_analyze_release_keeps_the_previous_bundle_when_refused(tmp_path, fake_cli, fake_gh):
    """A refused re-fetch (a mistyped --table-version) must not destroy the
    earlier fetch's analysis bundle."""
    from test_run import prepare

    prepare(tmp_path)
    assert run_cli(tmp_path, "run", "--level", "T1", "--settle-s", "0", "--reps", "1")[0] == 0
    run_id = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text())["run_id"]
    tag = f"run-{run_id}"
    assert run_cli(tmp_path, "release", "--run", run_id, "--repo", REPO)[0] == 0
    assert run_cli(tmp_path, "analyze", "--release", tag, "--repo", REPO)[0] == 0
    bundle = tmp_path / "releases" / tag / "analysis" / "analysis.json"
    assert bundle.exists()
    codigo, _, errores = run_cli(
        tmp_path, "analyze", "--release", tag, "--repo", REPO, "--table-version", "2026-09-01"
    )
    assert codigo == 2
    assert "table" in errores
    assert bundle.exists()  # the refused re-run left the fetched bundle alone


def test_analyze_release_refuses_a_foreign_level_or_model(tmp_path, fake_cli, fake_gh):
    """A filter the release cannot satisfy is a clean refusal, never a silent
    zero-cell bundle with exit 0."""
    from test_run import prepare

    prepare(tmp_path)
    assert run_cli(tmp_path, "run", "--level", "T1", "--settle-s", "0", "--reps", "1")[0] == 0
    run_id = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text())["run_id"]
    assert run_cli(tmp_path, "release", "--run", run_id, "--repo", REPO)[0] == 0
    tag = f"run-{run_id}"
    codigo, _, errores = run_cli(
        tmp_path, "analyze", "--release", tag, "--repo", REPO, "--level", "T2"
    )
    assert codigo == 2
    assert "level" in errores
    codigo, _, errores = run_cli(
        tmp_path, "analyze", "--release", tag, "--repo", REPO, "--model", "no-esta-modelo"
    )
    assert codigo == 2
    assert "no-esta-modelo" in errores


def test_release_scrubs_a_json_escaped_key(tmp_path, monkeypatch, fake_gh):
    """A key carrying characters JSON escapes (quotes, backslashes) reaches the
    dataset in escaped form (the raw bytes never appear): the scrub matches
    the escaped form too."""
    clave = 'ollama-live"secret\\777'
    monkeypatch.setenv("OLLAMA_API_KEY", clave)
    pricing = hand_dataset(tmp_path, poison=clave)  # the writer JSON-escapes it on disk
    codigo, _, errores = run_cli(
        tmp_path, "release", "--run", RUN, "--repo", REPO, "--pricing-dir", pricing
    )
    assert codigo == 2
    assert "credential" in errores
    assert gh_calls(fake_gh) == []


def test_status_still_accepts_the_run_flags(tmp_path):
    """status never reads --reps/--rep/--k but accepted them before Harness 10;
    scripts mirroring `run`'s shape keep parsing."""
    hand_dataset(tmp_path)
    codigo, _, errores = run_cli(tmp_path, "status", "--reps", "5", "--rep", "1", "--k", "4")
    assert codigo == 0, errores
