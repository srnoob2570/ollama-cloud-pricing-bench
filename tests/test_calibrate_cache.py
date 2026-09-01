"""`bench calibrate-cache` end-to-end against the fake: ticket Harness 07.

The cache-calibration workstream (methodology v1 §7): one fixed ~20K prefix
replayed per T2-slate model — a cold reference, r=4 intra-batch, and spaced
replays at increasing offsets — read through the three signals (reported
tokens, bracketed Δpp, TTFT). The calibration doc identifies cache existence,
persistence, the effective hit rate and the unmaterialized paper discounts;
`resolve_s` is the seam `analyze` consumes (measurement wins over S1, S0 as
the floor). Everything is asserted through produced artifacts and the requests
the fake observed.
"""

from __future__ import annotations

import json
import pathlib

from test_dry_run import run_cli, with_pricing
from test_run import read_jsonl

from ocharness.calibration import resolve_s
from ocharness.schema import validate_batch_line, validate_request_line

MODEL = "glm-5.3-flash"
# A tiny ladder the fake's horizons can split: the gaps between the spaced
# replays are (0.02, 0.02, 0.08), so a 0.05 s horizon expires on the third.
GAPS = ("0.02", "0.04", "0.12")


def prepare(tmp_path, *dry_extra) -> str:
    pricing = with_pricing(tmp_path)
    assert (
        run_cli(
            tmp_path,
            "dry-run",
            "--level",
            "T2",
            "--reps",
            "1",
            "--pricing-dir",
            pricing,
            *dry_extra,
        )[0]
        == 0
    )
    return pricing


def calibrate_cli(tmp_path, *extra) -> tuple[int, str, str]:
    return run_cli(
        tmp_path,
        "calibrate-cache",
        "--settle-s",
        "0",
        "--spaced-gaps",
        *GAPS,
        *extra,
    )


def always_hits(fake) -> None:
    """The caching world: hits persist, a hit bills fewer ticks, every reply
    satisfies the replay's one-word contract."""
    fake.reply_for = lambda prompt: "OK"
    fake.cache_horizon_s = 999.0
    fake.cached_eval_count = 6
    fake.ticks_per_request = 10
    fake.cached_ticks = 2


def chats(fake) -> list[dict]:
    return [c for c in fake.calls if c["path"] == "/api/chat"]


def reads(fake) -> list[dict]:
    return [c for c in fake.calls if c["path"] == "/api/usage"]


def summary(tmp_path) -> dict:
    files = sorted(pathlib.Path(tmp_path, "runs").glob("calibration-*.json"))
    assert len(files) == 1, f"expected exactly one calibration doc, got {files}"
    return json.loads(files[0].read_text(encoding="utf-8"))


def fresh_mark(tmp_path) -> None:
    """A fresh dry-run mark: the calibration consumes one per invocation."""
    assert (
        run_cli(
            tmp_path,
            "dry-run",
            "--level",
            "T2",
            "--reps",
            "1",
            "--pricing-dir",
            str(pathlib.Path(tmp_path) / "pricing"),
        )[0]
        == 0
    )


def test_persistence_survives_a_replay_without_token_evidence():
    """A failed spaced replay (no done: no token evidence) between misses must
    not crash the persistence bucket with an empty max() over hit indices."""
    from ocharness import calibration
    from ocharness.pricing import PriceTable

    tabla = PriceTable.load(pathlib.Path(__file__).resolve().parents[1] / "pricing")
    lineas = {
        "cache_cold": [
            {
                "tok_in": 26,
                "tok_cached": 0,
                "t_start": 0.0,
                "t_first_chunk": 0.05,
                "t_total": 0.1,
                "req_id": "b1-0000",
            }
        ],
        "cache_intra": [
            {
                "tok_in": 6,
                "tok_cached": 20,
                "t_start": 0.2,
                "t_first_chunk": 0.21,
                "t_total": 0.3,
                "req_id": f"b2-{i:04d}",
            }
            for i in range(4)
        ],
        "cache_spaced": [
            # request 0 failed (no done): no evidence either way
            {
                "tok_in": None,
                "tok_cached": None,
                "t_start": 1.0,
                "t_first_chunk": 1.01,
                "t_total": 1.1,
                "req_id": "b3-0000",
            },
            {
                "tok_in": 26,
                "tok_cached": 0,
                "t_start": 2.0,
                "t_first_chunk": 2.05,
                "t_total": 2.1,
                "req_id": "b3-0001",
            },
            {
                "tok_in": 26,
                "tok_cached": 0,
                "t_start": 3.0,
                "t_first_chunk": 3.05,
                "t_total": 3.1,
                "req_id": "b3-0002",
            },
        ],
    }
    brackets = {
        "cache_cold": {"batch_id": "b1", "dpp_weekly": 1.0},
        "cache_intra": {"batch_id": "b2", "dpp_weekly": 0.8},
        "cache_spaced": {"batch_id": "b3", "dpp_weekly": 0.6},
    }
    lectura = calibration._analyze_model("glm-5.3-flash", brackets, lineas, tabla=tabla)
    assert lectura["persistence"] is None  # a horizon needs evidence, not crashes
    assert [r["hit"] for r in lectura["signals"]["cache_spaced"]["replays"]] == [None, False, False]


def test_zero_token_reports_are_broken_telemetry_never_perfect_hits():
    """A warm replay reporting prompt_eval_count=0 is a broken report: it must
    contribute no sample (not a 1.0 hit rate) and no hit evidence."""
    from ocharness import calibration
    from ocharness.pricing import PriceTable

    tabla = PriceTable.load(pathlib.Path(__file__).resolve().parents[1] / "pricing")
    lineas = {
        "cache_cold": [
            {
                "tok_in": 26,
                "tok_cached": 0,
                "t_start": 0.0,
                "t_first_chunk": 0.05,
                "t_total": 0.1,
                "req_id": "b1-0000",
            }
        ],
        "cache_intra": [
            {
                "tok_in": 0,
                "tok_cached": None,
                "t_start": 0.2,
                "t_first_chunk": 0.21,
                "t_total": 0.3,
                "req_id": f"b2-{i:04d}",
            }
            for i in range(4)
        ],
        "cache_spaced": [
            {
                "tok_in": 0,
                "tok_cached": None,
                "t_start": 1.0,
                "t_first_chunk": 1.01,
                "t_total": 1.1,
                "req_id": f"b3-{i:04d}",
            }
            for i in range(3)
        ],
    }
    brackets = {
        "cache_cold": {"batch_id": "b1", "dpp_weekly": 1.0},
        "cache_intra": {"batch_id": "b2", "dpp_weekly": 0.8},
        "cache_spaced": {"batch_id": "b3", "dpp_weekly": 0.6},
    }
    lectura = calibration._analyze_model("glm-5.3-flash", brackets, lineas, tabla=tabla)
    # the broken reports never became 1.0 samples: the working signal (the
    # meter's dp) carries the reading instead, through the dp proxy
    assert lectura["hit_rate_basis"] == "dpp proxy"
    assert lectura["rule"]["estimates"] == [0.8, 0.8]
    assert abs(lectura["hit_rate"] - 0.8) < 1e-9 and lectura["conclusive"] is True
    assert all(r["hit"] is None for r in lectura["signals"]["cache_spaced"]["replays"])


def test_calibration_prefix_is_shared_with_long_context():
    """The replay's ~20K prefix is the long_context register's own opening span."""
    from ocharness import fixtures

    replay = fixtures.build("T2", "cache_cold", 1)[0].prompt
    larga = fixtures.build("T2", "long_context", 1)[0].prompt
    lineas_replay, lineas_larga = replay.splitlines(), larga.splitlines()
    assert lineas_replay[0].startswith("Cache calibration replay")  # its own header
    assert lineas_replay[-1] == "Reply with the single word: OK."
    prefijo = lineas_replay[2:552]
    assert len(prefijo) == 550
    assert prefijo == lineas_larga[2:552]  # byte-identical provenance
    assert len(" ".join(prefijo).split()) >= 14_000  # ~20K tokens of document
    for carga, n in (("cache_cold", 1), ("cache_intra", 4), ("cache_spaced", 3)):
        specs = fixtures.build("T2", carga, n)
        assert len(specs) == n and all(s.prompt == replay for s in specs)
    for carga, n in (("cache_cold", 2), ("cache_intra", 3), ("cache_x", 1)):
        try:
            fixtures.build("T2", carga, n)
        except ValueError:
            pass
        else:
            raise AssertionError(f"fixture guard missing for {carga}/{n}")


def test_refuses_without_a_t2_dry_run_mark(tmp_path, fake_cli):
    prepare(tmp_path)
    pathlib.Path(tmp_path, "runs", "gate-T2.json").unlink()  # the mark is gone
    code, _out, err = calibrate_cli(tmp_path)
    assert code == 2 and "dry-run" in err
    assert chats(fake_cli) == []  # nothing was billed
    fresh_mark_level_t1(tmp_path)
    code, _out, err = calibrate_cli(tmp_path)
    assert code == 2 and "dry-run" in err  # a T1 mark does not open the T2 gate
    assert chats(fake_cli) == []


def fresh_mark_level_t1(tmp_path) -> None:
    assert (
        run_cli(
            tmp_path,
            "dry-run",
            "--level",
            "T1",
            "--reps",
            "1",
            "--pricing-dir",
            str(pathlib.Path(tmp_path) / "pricing"),
        )[0]
        == 0
    )


def test_refuses_flags_it_does_not_read(tmp_path, fake_cli):
    prepare(tmp_path)
    for bandera in ("--s", "--reps", "--rep", "--k"):
        code, _out, err = calibrate_cli(tmp_path, bandera, "8")
        # --s prefix-matches --spaced-gaps/--settle-s and dies as ambiguous; the
        # rest are outright unrecognized. Either way: never a silent no-op.
        assert code == 2 and ("unrecognized" in err or "ambiguous" in err), bandera
    code, _out, err = run_cli(tmp_path, "calibrate-cache", "--level", "T2", "--settle-s", "0")
    assert code == 2 and "no --level" in err
    assert fake_cli.calls == []  # refused before any request


def test_spaced_gaps_are_validated(tmp_path, fake_cli):
    prepare(tmp_path)
    for malos in (["1", "2"], ["3", "2", "1"], ["0", "-1", "2"], ["0", "0", "0"]):
        code, _out, err = run_cli(
            tmp_path, "calibrate-cache", "--spaced-gaps", *malos, "--settle-s", "0"
        )
        assert code == 2 and "spaced-gaps" in err, malos
    assert fake_cli.calls == []


def test_settle_s_is_validated_and_ancla_is_refused(tmp_path, fake_cli):
    """--settle-s nan/inf/negative never reaches the runner (a nan would strand
    the bracket in_flight); --ancla is not the calibration's knob — no silent no-op."""
    prepare(tmp_path)
    for malo in ("nan", "inf", "-1"):
        code, _out, err = run_cli(tmp_path, "calibrate-cache", "--settle-s", malo)
        assert code == 2 and "settle-s" in err, malo
    code, _out, err = run_cli(tmp_path, "calibrate-cache", "--settle-s", "0", "--ancla", "1000")
    assert code == 2 and "unrecognized" in err
    assert fake_cli.calls == []  # refused before any request


def test_model_outside_the_slate_is_refused(tmp_path, fake_cli):
    prepare(tmp_path)
    code, _out, err = calibrate_cli(tmp_path, "--model", "no-existe")
    assert code == 2 and "T2 slate" in err
    assert fake_cli.calls == []  # refused before any request


def test_conclusive_cache_measurement_end_to_end(tmp_path, fake_cli):
    """All-replay hits + a resolved billing discount: cache yes, conclusive, S1 replaced."""
    always_hits(fake_cli)
    prepare(tmp_path)
    code, out, err = calibrate_cli(tmp_path, "--model", MODEL)
    assert code == 0, out or err
    requests = read_jsonl(tmp_path, "runs", "requests-*.jsonl")
    assert len(requests) == 8  # 1 cold + 4 intra + 3 spaced
    por_carga: dict[str, list[dict]] = {}
    for r in requests:
        assert r["level"] == "T2" and r["model"] == MODEL and r["k"] == 1
        assert r["checker"] == "pass"  # the replay's one-word contract
        por_carga.setdefault(r["workload"], []).append(r)
    assert sorted(por_carga) == ["cache_cold", "cache_intra", "cache_spaced"]
    fria = por_carga["cache_cold"][0]
    assert fria["tok_in"] == 26 and fria["tok_cached"] == 0  # the cold send
    # the intra primer hits too (the cold bracket primed the prefix seconds ago)
    assert all(r["tok_in"] == 6 and r["tok_cached"] == 20 for r in por_carga["cache_intra"])
    assert all(r["tok_in"] == 6 and r["tok_cached"] == 20 for r in por_carga["cache_spaced"])
    assert len({r["fixture_hash"] for r in requests}) == 3  # one hash per bracket

    batches = read_jsonl(tmp_path, "batches", "batches-*.jsonl")
    por_carga_b = {b["workload"]: b for b in batches}
    assert set(por_carga_b) == {"cache_cold", "cache_intra", "cache_spaced"}
    assert [por_carga_b[w]["n"] for w in ("cache_cold", "cache_intra", "cache_spaced")] == [1, 4, 3]
    # 10 ticks cold; the intra bracket bills 4 hits x 2; the spaced replays only hits
    assert abs(por_carga_b["cache_cold"]["dpp_session"] - 1.0) <= 0.1
    assert abs(por_carga_b["cache_intra"]["dpp_session"] - 0.8) <= 0.1
    assert abs(por_carga_b["cache_spaced"]["dpp_session"] - 0.6) <= 0.1
    assert "spaced replays" in por_carga_b["cache_spaced"]["notes"]

    doc = summary(tmp_path)
    assert doc["kind"] == "cache-calibration" and doc["level"] == "T2-cache"
    assert doc["models"] == [MODEL]
    lectura = doc["readings"][MODEL]
    assert lectura["cache_exists"] == "yes" and lectura["conclusive"] is True
    assert abs(lectura["hit_rate"] - 20 / 26) < 1e-3  # 6 of 26 tokens re-evaluated
    assert lectura["hit_rate_basis"] == "reported cache-hit tokens"
    assert lectura["rule"]["dp_signal_ticks"] == 8.0  # (1.0 - 0.8/4) / 0.1
    assert lectura["rule"]["iqr"][0] > 0  # the estimates agree above zero
    assert lectura["persistence"].startswith(">= ")  # every spaced replay hit
    assert float(lectura["persistence"][len(">= ") : -len(" s")]) < 1.0
    assert lectura["paper_discount"] == {"declared": True, "materialized": True}
    assert doc["unmaterialized_paper_discounts"] == []
    assert all(r["hit"] is True for r in lectura["signals"]["cache_spaced"]["replays"])
    assert len(reads(fake_cli)) == 9  # 3 brackets x (pre + count + post)


def test_no_cache_measurement_lands_at_the_s0_floor(tmp_path, fake_cli):
    """A deployment tracking hits and finding none: conclusive no, S0 floor, and
    the table's paper discount is declared unmaterialized."""
    fake_cli.reply_for = lambda prompt: "OK"
    fake_cli.cache_horizon_s = 0.0  # tracks hits, never grants one
    fake_cli.ticks_per_request = 10
    prepare(tmp_path)
    code, out, err = calibrate_cli(tmp_path, "--model", MODEL)
    assert code == 0, out or err
    doc = summary(tmp_path)
    lectura = doc["readings"][MODEL]
    assert lectura["cache_exists"] == "no" and lectura["conclusive"] is True
    assert lectura["hit_rate"] == 0.0
    assert lectura["persistence"] == "none observed"
    assert lectura["rule"]["dp_signal_ticks"] == 0.0  # every request bills the same
    assert doc["unmaterialized_paper_discounts"] == [MODEL]
    resueltos = resolve_s(doc, doc["models"], default_s=0.5)
    assert resueltos[MODEL].s == 0.0 and resueltos[MODEL].source == "measured"
    assert "S0 floor" in resueltos[MODEL].note


def test_horizon_expiry_shows_the_persistence_bucket(tmp_path, fake_cli):
    """The ladder 0.02/0.04/0.12 with a 0.05 s horizon: the first two replays
    hit (each refreshing the entry), the third's 0.08 s age exceeds it."""
    fake_cli.reply_for = lambda prompt: "OK"
    fake_cli.cache_horizon_s = 0.05
    fake_cli.cached_eval_count = 6
    fake_cli.ticks_per_request = 10
    fake_cli.cached_ticks = 2
    prepare(tmp_path)
    code, out, err = calibrate_cli(tmp_path, "--model", MODEL)
    assert code == 0, out or err
    requests = read_jsonl(tmp_path, "runs", "requests-*.jsonl")
    espaciadas = [r for r in requests if r["workload"] == "cache_spaced"]
    assert [r["tok_cached"] > 0 for r in espaciadas] == [True, True, False]
    doc = summary(tmp_path)
    lectura = doc["readings"][MODEL]
    assert lectura["persistence"].startswith("between ") and lectura["persistence"].endswith(" s")
    primera, ultima = lectura["persistence"][len("between ") : -len(" s")].split(" and ")
    assert float(primera) < float(ultima)
    assert lectura["conclusive"] is True and lectura["cache_exists"] == "yes"


def test_inconclusive_when_the_meter_cannot_resolve_the_discount(tmp_path, fake_cli):
    """Hits everywhere but no billing difference: the dp signal stays under the
    2-tick floor, so the measurement never overrides S1 — marked, not silent."""
    fake_cli.reply_for = lambda prompt: "OK"
    fake_cli.cache_horizon_s = 999.0
    fake_cli.cached_eval_count = 6
    fake_cli.ticks_per_request = 4
    fake_cli.cached_ticks = 4  # the hit bills the same: the meter cannot see it
    prepare(tmp_path)
    code, out, err = calibrate_cli(tmp_path, "--model", MODEL)
    assert code == 0, out or err
    doc = summary(tmp_path)
    lectura = doc["readings"][MODEL]
    assert lectura["cache_exists"] == "yes"  # token evidence exists
    assert lectura["conclusive"] is False and lectura["hit_rate"] is None
    assert lectura["rule"]["dp_signal_ticks"] <= 2.0
    resueltos = resolve_s(doc, doc["models"], default_s=0.5)
    assert resueltos[MODEL].s == 0.5 and resueltos[MODEL].source == "assumed"
    assert resueltos[MODEL].conclusive is False
    assert "inconclusive" in resueltos[MODEL].note and "marked" in resueltos[MODEL].note


def test_no_hit_field_world_reads_the_prompt_eval_drop(tmp_path, fake_cli):
    """A deployment that caches but never reports the hit field: the warm
    prompt_eval_count drop is the token evidence, and the measurement holds."""
    fake_cli.reply_for = lambda prompt: "OK"
    fake_cli.cache_horizon_s = 999.0
    fake_cli.cached_eval_count = 6
    fake_cli.cache_report_hits = False
    fake_cli.ticks_per_request = 10
    fake_cli.cached_ticks = 2
    prepare(tmp_path)
    code, _out, err = calibrate_cli(tmp_path, "--model", MODEL)
    assert code == 0, err
    requests = read_jsonl(tmp_path, "runs", "requests-*.jsonl")
    assert all("prompt_eval_cache_hit_count" not in r["api"] for r in requests)
    tibias = [r for r in requests if r["workload"] != "cache_cold"]
    assert all(r["tok_in"] == 6 for r in tibias) and all(r["tok_cached"] is None for r in tibias)
    doc = summary(tmp_path)
    lectura = doc["readings"][MODEL]
    assert lectura["cache_exists"] == "yes" and lectura["conclusive"] is True
    assert abs(lectura["hit_rate"] - 20 / 26) < 1e-3  # 1 - 6/26, from the drop alone
    assert lectura["hit_rate_basis"] == "prompt-eval drop"


def test_resolve_s_keeps_the_assumption_for_a_model_never_calibrated(tmp_path, fake_cli):
    always_hits(fake_cli)
    prepare(tmp_path)
    assert calibrate_cli(tmp_path, "--model", MODEL)[0] == 0
    doc = summary(tmp_path)
    resueltos = resolve_s(doc, [MODEL, "kimi-k3"], default_s=0.5)
    assert resueltos[MODEL].source == "measured"
    assert resueltos["kimi-k3"].s == 0.5 and resueltos["kimi-k3"].source == "assumed"
    assert "no calibration data" in resueltos["kimi-k3"].note
    assert resueltos["kimi-k3"].conclusive is False


def test_second_invocation_extends_the_doc_and_skips_calibrated_models(tmp_path, fake_cli):
    always_hits(fake_cli)
    prepare(tmp_path)
    assert calibrate_cli(tmp_path, "--model", MODEL)[0] == 0
    antes_chats, antes_reads = len(chats(fake_cli)), len(reads(fake_cli))
    fresh_mark(tmp_path)
    code, out, err = calibrate_cli(tmp_path, "--model", "kimi-k3")
    assert code == 0, out or err
    assert len(chats(fake_cli)) == antes_chats + 8  # only kimi-k3's brackets ran
    assert len(reads(fake_cli)) == antes_reads + 9
    doc = summary(tmp_path)
    assert doc["models"] == [MODEL, "kimi-k3"]  # the run's doc, not one model's
    assert doc["readings"][MODEL]["conclusive"] is True
    assert doc["readings"]["kimi-k3"]["conclusive"] is True


def test_unscripted_world_is_unknown_never_a_false_no(tmp_path, fake_cli):
    """No cache field, no token drop, no dp discount: the honest reading is
    'unknown' — an invisible cache cannot be ruled out, so S1 stays marked."""
    fake_cli.reply_for = lambda prompt: "OK"
    fake_cli.ticks_per_request = 10
    prepare(tmp_path)
    code, out, err = calibrate_cli(tmp_path, "--model", MODEL)
    assert code == 0, out or err
    doc = summary(tmp_path)
    lectura = doc["readings"][MODEL]
    assert lectura["cache_exists"] == "unknown" and lectura["conclusive"] is False
    assert lectura["hit_rate"] is None and lectura["persistence"] is None
    assert doc["unmaterialized_paper_discounts"] == []  # unknown is not "no"
    resueltos = resolve_s(doc, doc["models"], default_s=0.5)
    assert resueltos[MODEL].s == 0.5 and resueltos[MODEL].source == "assumed"


def test_resume_skips_an_in_flight_bracket_never_retries(tmp_path, fake_cli):
    always_hits(fake_cli)
    prepare(tmp_path)
    assert calibrate_cli(tmp_path, "--model", MODEL)[0] == 0
    antes = len(chats(fake_cli))
    ruta = pathlib.Path(tmp_path, "runs", "manifest-T2-cache.json")
    manifiesto = json.loads(ruta.read_text(encoding="utf-8"))
    victima = next(iter(manifiesto["batches"]))
    manifiesto["batches"][victima]["status"] = "in_flight"  # a crash mid-bracket
    ruta.write_text(json.dumps(manifiesto), encoding="utf-8")
    fresh_mark(tmp_path)
    code, out, err = calibrate_cli(tmp_path, "--model", MODEL)
    assert code == 0, out or err
    assert len(chats(fake_cli)) == antes  # nothing re-billed
    assert "in_flight" in (out + err)  # skipped loudly, never silently retried


def test_gap_plan_drift_is_refused(tmp_path, fake_cli):
    prepare(tmp_path)
    assert calibrate_cli(tmp_path, "--model", MODEL)[0] == 0
    ruta = pathlib.Path(tmp_path, "runs", "manifest-T2-cache.json")
    manifiesto = json.loads(ruta.read_text(encoding="utf-8"))
    manifiesto["gap_plan"] = {"targets": [1.0, 2.0, 3.0], "repeats": 4}  # as if changed
    ruta.write_text(json.dumps(manifiesto), encoding="utf-8")
    fresh_mark(tmp_path)
    antes = len(chats(fake_cli))
    code, _out, err = calibrate_cli(tmp_path, "--model", MODEL)
    assert code == 1 and "gap plan" in err and "Traceback" not in err
    assert len(chats(fake_cli)) == antes  # refused before any request


def test_raw_lines_honor_the_schemas_and_leak_no_key(tmp_path, fake_cli):
    always_hits(fake_cli)
    prepare(tmp_path)
    assert calibrate_cli(tmp_path, "--model", MODEL)[0] == 0
    for r in read_jsonl(tmp_path, "runs", "requests-*.jsonl"):
        validate_request_line(r)
    for b in read_jsonl(tmp_path, "batches", "batches-*.jsonl"):
        validate_batch_line(b)
    doc = summary(tmp_path)
    assert doc["protocol_version"] and doc["table_version"] == "2026-08-31"
    for carpeta in ("runs", "batches"):
        for ruta in pathlib.Path(tmp_path, carpeta).iterdir():
            assert "test-key" not in ruta.read_text(encoding="utf-8"), ruta.name


def test_status_shows_the_calibration_run(tmp_path, fake_cli):
    always_hits(fake_cli)
    prepare(tmp_path)
    assert calibrate_cli(tmp_path, "--model", MODEL)[0] == 0
    code, out, _err = run_cli(tmp_path, "status", "--level", "T2-cache")
    assert code == 0
    assert "3 done" in out and "T2-cache" in out


def test_meter_failure_mid_calibration_keeps_the_billed_evidence(tmp_path, fake_cli):
    """A meter drop after the intra bracket aborts loudly; the raw evidence stays."""
    import httpx

    always_hits(fake_cli)
    prepare(tmp_path)
    # reads: cold pre/count/post, intra pre/count/post, then the spaced pre dies
    fake_cli.usage_raise = httpx.ConnectError("meter dropped")
    fake_cli.usage_raise_from = 7
    code, _out, err = calibrate_cli(tmp_path, "--model", MODEL)
    assert code == 1
    assert "meter read failed" in err and "Traceback" not in err
    requests = read_jsonl(tmp_path, "runs", "requests-*.jsonl")
    assert len(requests) == 5  # the cold + intra brackets' billed evidence, kept
    batches = read_jsonl(tmp_path, "batches", "batches-*.jsonl")
    assert [b["workload"] for b in batches] == ["cache_cold", "cache_intra"]
    manifiesto = json.loads(
        pathlib.Path(tmp_path, "runs", "manifest-T2-cache.json").read_text(encoding="utf-8")
    )
    assert [e["status"] for e in manifiesto["batches"].values()].count("done") == 2


def test_human_report_renders_an_incomplete_reading(tmp_path, fake_cli):
    """A model whose spaced bracket never closed still reports its hit rate —
    the intra replays carry it — with the persistence honestly unknown."""
    import httpx

    always_hits(fake_cli)
    prepare(tmp_path)
    fake_cli.usage_raise = httpx.ConnectError("meter dropped")
    fake_cli.usage_raise_from = 7  # the spaced bracket's pre-read dies
    assert calibrate_cli(tmp_path, "--model", MODEL)[0] == 1
    fake_cli.usage_raise = None
    fresh_mark(tmp_path)
    code, out, err = calibrate_cli(tmp_path, "--model", MODEL)  # resume: all closed
    assert code == 0, out or err
    assert "persistence unknown" in out and "76.9%" in out
    doc = summary(tmp_path)
    lectura = doc["readings"][MODEL]
    assert lectura["cache_exists"] == "yes" and lectura["conclusive"] is True
    assert lectura["persistence"] is None  # the missing bracket loses only the horizon
    assert "cache_spaced bracket never closed" in lectura["notes"]
