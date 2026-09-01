"""Deterministic T3 fixtures: the three synthetic mini-repos (methodology v1 §5).

Every workload is a small Python project whose pytest suite fails until a known,
canonical fix lands; the fixture IS the repo — every file seeded byte-for-byte from
the fixed in-repo seed (`T3_SEED`), hash-stamped on the batch, and written into an
isolated working copy per task. The task prompt carries the goal plus the loop's
single-JSON-action contract; the checker later restores the protected files (the
tests and their conftest) before grading, so the model's edits to the suite can
never make a broken task pass. English, synthetic, version-in-repo.
"""

from __future__ import annotations

import random

# Fixture version marker of the T3 generators: bumping it re-rolls every mini-repo.
T3_SEED = 20260901

# The loop's action budget (methodology v1 §5): at most 12 model consultations.
MAX_STEPS = 12

# Fixed requests-per-repetition shape per workload (workloads.T3): a generator
# called with another n is fixture drift, not a smaller workload.
_N_BY_WORKLOAD = {
    "multi_file": 1,
    "debugging": 1,
    "refactoring": 1,
}

WORKLOADS = tuple(_N_BY_WORKLOAD)

_CONFTEST = '''"""Puts the project root on sys.path so the tests import the package directly."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
'''

# The single-JSON-action contract every task prompt ends with: the loop's protocol,
# shared by the three workloads so the scripted and the real transcripts agree.
_ACTION_CONTRACT = (
    "You work inside an isolated sandbox directory that already contains this project. "
    "You have no shell: each turn, reply with exactly ONE JSON object and nothing else, "
    "choosing one action:\n"
    "\n"
    '{"action": "list_dir", "path": "<relative path>"}\n'
    '{"action": "read_file", "path": "<relative path>"}\n'
    '{"action": "write_file", "path": "<relative path>", "content": "<full new content>"}\n'
    '{"action": "apply_patch", "path": "<relative path>", "search": "<exact existing text>", '
    '"replace": "<new text>"}\n'
    '{"action": "run_tests"}\n'
    '{"action": "finish"}\n'
    "\n"
    "`apply_patch` replaces the FIRST occurrence of `search` in the file with `replace`. "
    "Relative paths stay inside the project: `..` and absolute paths are rejected. "
    "`run_tests` runs the project's pytest suite. `finish` ends the session - call it "
    "only after `run_tests` has shown every test passing."
)

_DEBUGGING_TASK = (
    "Sandbox task (debugging). The `plantmon` package in your working directory ships "
    "a failing test suite. Run the tests, work out the behavior they demand, and fix "
    "the package's source - never the tests - so the whole suite passes."
)

_MULTI_FILE_TASK = (
    "Sandbox task (multi_file). The `warehouse` package in your working directory "
    "ships a failing test suite: `tests/test_reservations.py` specifies a reservations "
    "feature the package must grow. Build it so the whole suite passes: the "
    "reservations code belongs in a new `warehouse/reservations.py` module, the "
    "operational cap belongs in a new `warehouse/limits.py`, and `Reservation` and "
    "`MAX_ACTIVE_RESERVATIONS` must be importable from the `warehouse` package itself. "
    "Never modify the tests."
)

_REFACTORING_TASK = (
    "Sandbox task (refactoring). The `kilnlog` package in your working directory "
    "renders kiln shift reports. `tests/test_format_reading.py` requires a public "
    "`format_reading(reading) -> str` in `kilnlog/report.py` that renders ONE reading's "
    "line and rejects an empty unit with ValueError. Extract it from `build_report`, "
    "which must keep its exact behavior and delegate every line to the new function, "
    "so the whole suite passes. Never modify the tests."
)


def _rng(workload: str) -> random.Random:
    """One generator per workload: adding a workload never shifts another's bytes."""
    return random.Random(f"ocharness-t3|{T3_SEED}|{workload}")


# ---------------------------------------------------------------------
# debugging: `plantmon` — an off-by-one rolling mean; the seeded reference
# batch gives the suite a data file whose window count the bug breaks.

_SENSORS_BUGGY = '''"""Rolling statistics over plant sensor readings."""

from __future__ import annotations

import statistics


def moving_average(samples: list[float], window: int) -> list[float]:
    """One mean per possible window of `window` consecutive samples, in order."""
    if window < 1:
        raise ValueError("window must be at least 1")
    if window > len(samples):
        return []
    return [
        round(statistics.fmean(samples[start : start + window]), 6)
        for start in range(len(samples) - window)
    ]
'''

# The canonical fix: one patch, exactly the loop's apply_patch shape.
_SENSORS_FIX = (
    "plantmon/sensors.py",
    "        for start in range(len(samples) - window)\n",
    "        for start in range(len(samples) - window + 1)\n",
)

_SENSORS_TESTS = '''"""Behavior contract of the plantmon rolling statistics."""

import pathlib

import pytest

from plantmon.sensors import moving_average

DATA = (
    pathlib.Path(__file__).resolve().parents[1]
    / "plantmon"
    / "data"
    / "reference_batch.txt"
)


def test_one_mean_per_possible_window_position():
    assert moving_average([1.0, 2.0, 3.0, 4.0], 2) == [1.5, 2.5, 3.5]


def test_a_window_of_one_is_the_identity():
    assert moving_average([4.0, 5.5, 7.0], 1) == [4.0, 5.5, 7.0]


def test_a_full_size_window_is_a_single_mean():
    assert moving_average([2.0, 4.0, 6.0], 3) == [4.0]


def test_a_window_larger_than_the_batch_is_empty():
    assert moving_average([1.0], 2) == []


def test_a_nonpositive_window_is_rejected():
    with pytest.raises(ValueError):
        moving_average([1.0, 2.0], 0)


def test_the_reference_batch_means_match_the_data():
    import statistics

    muestras = [float(x) for x in DATA.read_text(encoding="utf-8").split()]
    esperadas = [
        round(statistics.fmean(muestras[i : i + 4]), 6) for i in range(len(muestras) - 3)
    ]
    assert moving_average(muestras, 4) == esperadas
'''


def _debugging_repo() -> tuple[tuple[str, str], ...]:
    rng = _rng("debugging")
    lecturas = "\n".join(f"{round(rng.uniform(-20.0, 120.0), 1)}" for _ in range(60))
    return (
        ("conftest.py", _CONFTEST),
        ("plantmon/__init__.py", '"""Sensor statistics for plant monitoring."""\n'),
        ("plantmon/sensors.py", _SENSORS_BUGGY),
        ("plantmon/data/reference_batch.txt", lecturas + "\n"),
        ("tests/test_sensors.py", _SENSORS_TESTS),
    )


# ---------------------------------------------------------------------
# multi_file: `warehouse` — the reservations feature spans a new module, a
# new limits file, and the package's public re-exports.

_WAREHOUSE_INIT = '''"""Warehouse stock management toolkit."""

from .catalog import CATALOG, Part
from .inventory import add_stock, stock_of

__all__ = ["CATALOG", "Part", "add_stock", "stock_of"]
'''

_WAREHOUSE_CATALOG = '''"""The parts catalog: every SKU the warehouse knows about."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Part:
    sku: str
    name: str
    unit_price: float


CATALOG: dict[str, Part] = {
    part.sku: part
    for part in (
        Part("BR-2214", "drive belt", 9.5),
        Part("SN-0421", "temperature sensor", 24.0),
        Part("VL-7780", "bypass valve", 61.25),
    )
}
'''

_WAREHOUSE_INVENTORY = '''"""Stock levels per SKU."""

from __future__ import annotations

from .catalog import CATALOG

STOCK: dict[str, int] = {"BR-2214": 40, "SN-0421": 12, "VL-7780": 5}


def stock_of(sku: str) -> int:
    """Current stock of one SKU; unknown SKUs raise KeyError."""
    if sku not in CATALOG:
        raise KeyError(f"unknown SKU: {sku!r}")
    return STOCK.get(sku, 0)


def add_stock(sku: str, quantity: int) -> int:
    """Adds stock and returns the new level; negative quantities are rejected."""
    if sku not in CATALOG:
        raise KeyError(f"unknown SKU: {sku!r}")
    if quantity < 0:
        raise ValueError("quantity must not be negative")
    STOCK[sku] = STOCK.get(sku, 0) + quantity
    return STOCK[sku]
'''

_WAREHOUSE_STOCK_TESTS = '''"""The package's existing behavior must keep passing."""

import pytest

from warehouse import add_stock, stock_of


def test_stock_of_a_known_sku():
    assert stock_of("BR-2214") == 40


def test_stock_of_an_unknown_sku_raises_keyerror():
    with pytest.raises(KeyError):
        stock_of("ZZ-0000")


def test_add_stock_rejects_a_negative_quantity():
    with pytest.raises(ValueError):
        add_stock("BR-2214", -1)
'''

_WAREHOUSE_RESERVATION_TESTS = '''"""The reservations feature the package must grow (suite fails until it exists)."""

import pytest

from warehouse import MAX_ACTIVE_RESERVATIONS, Reservation
from warehouse.reservations import Ledger, reserve


def test_reserve_returns_a_reservation_for_a_stocked_part():
    r = reserve("BR-2214", 2)
    assert isinstance(r, Reservation)
    assert r.sku == "BR-2214" and r.quantity == 2


def test_reserve_rejects_unknown_skus():
    with pytest.raises(KeyError):
        reserve("ZZ-0000", 1)


def test_reserve_rejects_quantities_above_stock():
    with pytest.raises(ValueError):
        reserve("SN-0421", 999)


def test_the_ledger_enforces_the_active_cap():
    libro = Ledger()
    for _ in range(MAX_ACTIVE_RESERVATIONS):
        libro.add(reserve("BR-2214", 1))
    with pytest.raises(RuntimeError):
        libro.add(reserve("BR-2214", 1))
'''

_LIMITS_FILE = '''"""Operational limits of the warehouse."""

MAX_ACTIVE_RESERVATIONS = 4
'''

_RESERVATIONS_FILE = '''"""Reservations against the warehouse stock."""

from __future__ import annotations

from .catalog import CATALOG
from .inventory import stock_of
from .limits import MAX_ACTIVE_RESERVATIONS


class Reservation:
    """One reserved quantity of one SKU."""

    def __init__(self, sku: str, quantity: int) -> None:
        self.sku = sku
        self.quantity = quantity


def reserve(sku: str, quantity: int) -> Reservation:
    """Builds a reservation after validating the SKU and the stock."""
    if sku not in CATALOG:
        raise KeyError(f"unknown SKU: {sku!r}")
    if quantity > stock_of(sku):
        raise ValueError(f"requested {quantity}, only {stock_of(sku)} in stock")
    return Reservation(sku, quantity)


class Ledger:
    """Holds active reservations up to the warehouse's cap."""

    def __init__(self, cap: int | None = None) -> None:
        self.cap = MAX_ACTIVE_RESERVATIONS if cap is None else cap
        self.active: list[Reservation] = []

    def add(self, reservation: Reservation) -> None:
        if len(self.active) >= self.cap:
            raise RuntimeError("the active reservation cap is reached")
        self.active.append(reservation)
'''

_INIT_PATCH_SEARCH = """from .catalog import CATALOG, Part
from .inventory import add_stock, stock_of

__all__ = ["CATALOG", "Part", "add_stock", "stock_of"]
"""

_INIT_PATCHED = """from .catalog import CATALOG, Part
from .inventory import add_stock, stock_of
from .limits import MAX_ACTIVE_RESERVATIONS
from .reservations import Ledger, Reservation

__all__ = [
    "CATALOG",
    "MAX_ACTIVE_RESERVATIONS",
    "Part",
    "Reservation",
    "add_stock",
    "stock_of",
]
"""


def _multi_file_repo() -> tuple[tuple[str, str], ...]:
    return (
        ("conftest.py", _CONFTEST),
        ("warehouse/__init__.py", _WAREHOUSE_INIT),
        ("warehouse/catalog.py", _WAREHOUSE_CATALOG),
        ("warehouse/inventory.py", _WAREHOUSE_INVENTORY),
        ("tests/test_stock.py", _WAREHOUSE_STOCK_TESTS),
        ("tests/test_reservations.py", _WAREHOUSE_RESERVATION_TESTS),
    )


# ---------------------------------------------------------------------
# refactoring: `kilnlog` — extract the per-reading renderer; the existing
# behavior must survive and the new public function must exist.

_KILNLOG_INIT = '''"""Kiln shift reporting."""

from .report import build_report

__all__ = ["build_report"]
'''

_KILNLOG_REPORT_INITIAL = '''"""Shift report rendering for the kiln line."""


def build_report(readings) -> str:
    """One line per (unit, celsius) reading: '<unit> reads <c> degrees'."""
    lineas = []
    for unit, celsius in readings:
        lineas.append(f"{unit} reads {celsius:.1f} degrees")
    return "\\n".join(lineas)
'''

_KILNLOG_REPORT_TESTS = '''"""The existing behavior the refactor must preserve."""

from kilnlog.report import build_report


def test_one_line_per_reading():
    report = build_report([("T1", 812.5), ("T2", 845.0)])
    assert report.splitlines() == ["T1 reads 812.5 degrees", "T2 reads 845.0 degrees"]


def test_an_empty_log_is_an_empty_report():
    assert build_report([]) == ""
'''

_KILNLOG_FORMAT_TESTS = '''"""The extraction the refactor must deliver (suite fails until it exists)."""

import pytest

from kilnlog.report import format_reading


def test_format_reading_renders_one_line():
    assert format_reading(("T1", 812.5)) == "T1 reads 812.5 degrees"


def test_format_reading_rejects_an_empty_unit():
    with pytest.raises(ValueError):
        format_reading(("", 800.0))
'''

_REPORT_FIX = (
    "kilnlog/report.py",
    '''def build_report(readings) -> str:
    """One line per (unit, celsius) reading: '<unit> reads <c> degrees'."""
    lineas = []
    for unit, celsius in readings:
        lineas.append(f"{unit} reads {celsius:.1f} degrees")
    return "\\n".join(lineas)
''',
    '''def format_reading(reading) -> str:
    """One line for one (unit, celsius) reading; an empty unit is rejected."""
    unit, celsius = reading
    if not unit:
        raise ValueError("the unit must not be empty")
    return f"{unit} reads {celsius:.1f} degrees"


def build_report(readings) -> str:
    """One line per (unit, celsius) reading: '<unit> reads <c> degrees'."""
    return "\\n".join(format_reading(reading) for reading in readings)
''',
)


def _refactoring_repo() -> tuple[tuple[str, str], ...]:
    return (
        ("conftest.py", _CONFTEST),
        ("kilnlog/__init__.py", _KILNLOG_INIT),
        ("kilnlog/report.py", _KILNLOG_REPORT_INITIAL),
        ("tests/test_report.py", _KILNLOG_REPORT_TESTS),
        ("tests/test_format_reading.py", _KILNLOG_FORMAT_TESTS),
    )


# ---------------------------------------------------------------------
# Dispatch

_REPOS = {
    "multi_file": _multi_file_repo,
    "debugging": _debugging_repo,
    "refactoring": _refactoring_repo,
}

_TASKS = {
    "multi_file": _MULTI_FILE_TASK,
    "debugging": _DEBUGGING_TASK,
    "refactoring": _REFACTORING_TASK,
}

# Unique opening of every T3 task prompt (workload_of relies on it).
_TASK_OPENING = "Sandbox task ("


def workload_of(prompt: str) -> str:
    """Which T3 workload generated this prompt (every task opens with its name)."""
    for workload in _N_BY_WORKLOAD:
        if prompt.startswith(f"Sandbox task ({workload})."):
            return workload
    raise ValueError(f"prompt carries no T3 task opening: {prompt[:60]!r}")


def specs(workload: str, n: int) -> list[tuple[str, tuple[tuple[str, str], ...]]]:
    """The workload's `n` deterministic (task prompt, repo files) pairs.

    Raises ValueError on an unknown workload or a wrong request count: the
    workload table's shape is part of the fixture contract.
    """
    if workload not in _N_BY_WORKLOAD:
        raise ValueError(f"unknown T3 workload: {workload!r}")
    if n != _N_BY_WORKLOAD[workload]:
        raise ValueError(
            f"T3 workload {workload!r} runs {_N_BY_WORKLOAD[workload]} request(s) per "
            f"repetition, not {n}"
        )
    contrato = f"{_TASKS[workload]}\n\n{_ACTION_CONTRACT}"
    return [(contrato, _REPOS[workload]())]


def fix_steps(workload: str) -> list[dict]:
    """The canonical fix as loop actions — the scripted transcript's edits."""
    if workload == "debugging":
        ruta, busca, reemplazo = _SENSORS_FIX
        return [{"action": "apply_patch", "path": ruta, "search": busca, "replace": reemplazo}]
    if workload == "multi_file":
        return [
            {"action": "write_file", "path": "warehouse/limits.py", "content": _LIMITS_FILE},
            {
                "action": "write_file",
                "path": "warehouse/reservations.py",
                "content": _RESERVATIONS_FILE,
            },
            {
                "action": "apply_patch",
                "path": "warehouse/__init__.py",
                "search": _INIT_PATCH_SEARCH,
                "replace": _INIT_PATCHED,
            },
        ]
    if workload == "refactoring":
        ruta, busca, reemplazo = _REPORT_FIX
        return [{"action": "apply_patch", "path": ruta, "search": busca, "replace": reemplazo}]
    raise ValueError(f"unknown T3 workload: {workload!r}")


def repo_files(workload: str) -> tuple[tuple[str, str], ...]:
    """The mini-repo's full seeded file set: what the checker grades against.

    The graded copy is rebuilt from these bytes, so the model's edits to the
    suite (or its own added config) can never decide a verdict — only its
    carried source changes can.
    """
    if workload not in _REPOS:
        raise ValueError(f"unknown T3 workload: {workload!r}")
    return _REPOS[workload]()
