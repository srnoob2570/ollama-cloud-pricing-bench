"""Deterministic T2 fixtures: the 7 structural suites (methodology v1 §5).

Every generator derives from one fixed, in-repo seed (`T2_SEED`): the same
bytes on every machine and every repetition, so a cell serves the same load on
every slate model and the batch hash pins it forever. The prompt is the
contract — it carries the load (register, transcript, tool scenario) plus the
instructions whose structure the checker grades; the parse helpers that
re-derive an answer key from a generated prompt also live here, so fixture and
checker read the same grammar. English, synthetic, version-in-repo.
"""

from __future__ import annotations

import random
import re

# Fixture version: bumping it re-rolls every T2 fixture (a new fixture_hash).
T2_SEED = 20260831

# Fixed requests-per-repetition shape per workload (workloads.T2): a generator
# called with another n is fixture drift, not a smaller workload.
_N_BY_WORKLOAD = {
    "long_context": 1,
    "long_generation": 1,
    "multi_turn": 8,
    "tool_calling": 6,
    "reasoning": 1,
    "ratio_in": 1,
    "ratio_out": 20,
}

# Shared sentence bank for padding: words only — no digits (one could collide
# with a graded datum) and never the word "answer" (the reasoning contract).
_PAD_SENTENCES = (
    "the coupling was tightened to the marked torque",
    "the housing shows no unusual wear for its age",
    "vibration stayed inside the recorded band all week",
    "the gasket was replaced during the last overhaul",
    "filters were swapped at the start of the shift",
    "the interlock test finished without interventions",
    "lubrication follows the seasonal chart pinned in the office",
    "the bypass valve remains sealed until further notice",
    "spare seals are stored in the annex beside the stairs",
    "the night crew logs every reset in the paper journal",
    "temperature drifts were noted only after long runs",
    "the frame anchors were retorqued after the storm",
)


def _rng(workload: str) -> random.Random:
    """One generator per workload: adding a workload never shifts another's bytes."""
    return random.Random(f"obench-t2|{T2_SEED}|{workload}")


# ---------------------------------------------------------------------
# Register workloads (long_context, ratio_in): one labeled datum per line.
# The line grammar below is the single source of truth for generator and
# checker — both parse the same fixed shape, so the buried datum is always
# recoverable from the prompt alone.

_REGISTER_LINE = (
    "[R-{label:04d}] {device} — access code {code} — inspected every {days} days "
    "by the {shift} crew of plant {plant}; {pad}."
)
_REGISTER_HEADER = (
    "Maintenance register, quarterly audit copy. Every unit line carries its "
    "access code and its inspection cadence."
)
_REGISTER_TASK = "From the register above, answer each item in one short sentence, in order:"
_RE_REGISTER_LINE = re.compile(
    r"^\[R-(\d{4})\] .+? — access code ([A-Z]{2}-\d{4}-[A-Z]{2}) — inspected every (\d+) days",
    re.MULTILINE,
)
_RE_ASK_CODE = re.compile(r"\d+\) What is the access code of the unit tagged \[R-(\d{4})\]\?")
_RE_ASK_DAYS = re.compile(
    r"\d+\) How many days pass between inspections of the unit tagged \[R-(\d{4})\]\?"
)

# The reply must stay one short line AND name its unit: the register checker
# anchors every datum to the sentence that carries it (a right value attached
# to no unit cannot be graded), so "code only, nothing else" was an
# unsatisfiable instruction — every model obeyed it and failed 30/30 (#audit
# 2026-09-02, OBSERVACIÓN 9).
_REGISTER_REPLY_ONLY = (
    "Reply in one short sentence: name the unit and its access code, nothing else."
)

_DEVICES = (
    "coolant pump",
    "conveyor belt",
    "impact crusher",
    "tunnel kiln",
    "air compressor",
    "steam boiler",
    "extruder",
    "paint booth",
    "cooling tower",
    "packaging line",
)
_SHIFTS = ("morning", "evening", "night")
_PLANTS = ("north", "south", "east", "west")
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWZ"


def _unique_codes(rng: random.Random, count: int) -> list[str]:
    codes: set[str] = set()
    while len(codes) < count:
        letras = "".join(rng.choice(_LETTERS) for _ in range(4))
        codes.add(f"{letras[:2]}-{rng.randrange(10000):04d}-{letras[2:]}")
    return sorted(codes)


def _register_text(rng: random.Random, lines: int, asks: list[tuple[int, str]]) -> str:
    """A register of `lines` units plus the task block asking (label, code|days)."""
    codigos = _unique_codes(rng, lines)
    unidades = [
        _REGISTER_LINE.format(
            label=i + 1,
            device=_DEVICES[i % len(_DEVICES)],
            code=codigos[i],
            days=rng.randrange(30, 361, 30),
            shift=rng.choice(_SHIFTS),
            plant=rng.choice(_PLANTS),
            pad=rng.choice(_PAD_SENTENCES),
        )
        for i in range(lines)
    ]
    preguntas = "\n".join(
        f"{i}) "
        + (
            f"What is the access code of the unit tagged [R-{label:04d}]?"
            if campo == "code"
            else f"How many days pass between inspections of the unit tagged [R-{label:04d}]?"
        )
        for i, (label, campo) in enumerate(asks, 1)
    )
    return f"{_REGISTER_HEADER}\n\n" + "\n".join(unidades) + f"\n\n{_REGISTER_TASK}\n{preguntas}"


def register_datums(prompt: str) -> dict[str, dict[str, str]]:
    """The register's labeled datums: {label: {"code": ..., "days": ...}}.

    Raises ValueError on a prompt without parsable register lines: fixture
    drift is a harness bug, never a model verdict.
    """
    datums = {
        label: {"code": code, "days": days}
        for label, code, days in _RE_REGISTER_LINE.findall(prompt)
    }
    if not datums:
        raise ValueError("register prompt carries no parsable unit lines")
    return datums


def register_asks(prompt: str) -> list[tuple[str, str]]:
    """The task block's (label, field) pairs in question order (field: code|days)."""
    items: list[tuple[str, str]] = []
    for linea in prompt.splitlines():
        if m := _RE_ASK_CODE.fullmatch(linea.strip()):
            items.append((m.group(1), "code"))
        elif m := _RE_ASK_DAYS.fullmatch(linea.strip()):
            items.append((m.group(1), "days"))
    if not items:
        raise ValueError("register prompt carries no parsable task block")
    return items


def _long_context(rng: random.Random) -> list[tuple[str, tuple[dict, ...]]]:
    asks = [(311, "code"), (87, "days"), (901, "code")]
    texto = _register_text(rng, lines=950, asks=asks)  # ~30K tokens of document
    return [(f"{texto}\nKeep every answer to one short sentence.", ())]


def _ratio_in(rng: random.Random) -> list[tuple[str, tuple[dict, ...]]]:
    # One datum buried in a very large input; the answer is one short line
    # that names the unit (the checker anchors the datum to its label).
    texto = _register_text(rng, lines=1600, asks=[(1500, "code")])
    return [(f"{texto}\n{_REGISTER_REPLY_ONLY}", ())]


# ---------------------------------------------------------------
# multi_turn: an accumulating shift log — 8 turns, each prompt carrying
# the full transcript so far, so tok_in grows every turn.

_MT_UNITS = ("pump", "conveyor", "crusher", "kiln", "compressor", "boiler", "extruder", "cooler")
_MULTI_TURN_HEADER = (
    "You are the on-call assistant reading a plant shift log. Answer the final "
    "question only, in one short sentence, using only the log."
)
_MULTI_TURN_PREAMBLE = (
    "Standing note for every shift: the log is the single source of truth, and "
    "the office keeps the paper copy locked overnight."
)


def _multi_turn(rng: random.Random) -> list[tuple[str, tuple[dict, ...]]]:
    codigos = _unique_codes(rng, len(_MT_UNITS))
    codigo_de = dict(zip(_MT_UNITS, codigos))
    turnos: list[tuple[str, str]] = []  # (unit online this turn, unit its question asks about)
    for t in range(1, len(_MT_UNITS) + 1):
        preguntada = rng.randrange(1, t + 1)  # the question may ask about any line so far
        turnos.append((_MT_UNITS[t - 1], _MT_UNITS[preguntada - 1]))
    prompts: list[str] = []
    for t in range(1, len(turnos) + 1):
        lineas = []
        for i, (unit, preguntada) in enumerate(turnos[:t], 1):
            detalle = rng.choice(_PAD_SENTENCES)
            hecho = f"[Turn {i}] operator: the {unit} line came online; access code {codigo_de[unit]}; {detalle}."
            if i < t:
                lineas.append(hecho)
                # A past answer quotes the code of the unit its question asked about —
                # the transcript must stay true, or an attentive model is misled.
                respondida = turnos[i - 1][1]
                lineas.append(
                    f"[Turn {i}] assistant: The access code of the {respondida} line is "
                    f"{codigo_de[respondida]}."
                )
            else:
                lineas.append(f"{hecho} What is the access code of the {preguntada} line?")
        prompts.append(f"{_MULTI_TURN_HEADER}\n{_MULTI_TURN_PREAMBLE}\n\n" + "\n".join(lineas))
    return [(p, ()) for p in prompts]


def multi_turn_expected(prompt: str) -> str:
    """The access code the final turn's question asks for (checker + test helper)."""
    preguntadas = re.findall(r"access code of the (\w+) line\?", prompt)
    if not preguntadas:
        raise ValueError("multi_turn prompt carries no final question")
    preguntada = preguntadas[-1]
    hechos = dict(
        re.findall(r"the (\w+) line came online; access code ([A-Z]{2}-\d{4}-[A-Z]{2})", prompt)
    )
    if preguntada not in hechos:
        raise ValueError(f"multi_turn prompt asks about unknown unit {preguntada!r}")
    return hechos[preguntada]


# ---------------------------------------------------------------------
# tool_calling: 6 scenarios with declared JSON-schema tools. The expected
# call sequence and the schemas live in this one table — fixture and checker
# read it, so they can never disagree about what a valid call is.

_TOOL_DESK_HEADER = (
    "Tool desk request {tid}. Use the available tools for this task and reply with "
    "the tool calls only."
)

_TEMPERATURE_UNIT = {"type": "string", "enum": ["celsius", "fahrenheit"]}


def _tool_decl(name: str, description: str, properties: dict, required: tuple) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(required),
            },
        },
    }


def _tool_scenarios() -> dict[str, dict]:
    clima = _tool_decl(
        "weather_lookup",
        "Current weather for a city",
        {"city": {"type": "string"}, "unit": dict(_TEMPERATURE_UNIT)},
        ("city", "unit"),
    )
    conversion = _tool_decl(
        "unit_convert",
        "Convert a temperature value between units",
        {
            "value": {"type": "number"},
            "from_unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
            "to_unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
        },
        ("value", "from_unit", "to_unit"),
    )
    inventario = _tool_decl(
        "inventory_lookup",
        "Stock level of one part in one warehouse",
        {"sku": {"type": "string"}, "warehouse": {"type": "string", "enum": ["west", "east"]}},
        ("sku", "warehouse"),
    )
    repuesto = _tool_decl(
        "order_replacement",
        "Order a replacement part",
        {"sku": {"type": "string"}, "quantity": {"type": "integer", "minimum": 1, "maximum": 9}},
        ("sku", "quantity"),
    )
    sala = _tool_decl(
        "room_reserve",
        "Reserve a meeting room",
        {
            "room": {"type": "string", "enum": ["aurora", "borealis"]},
            "date": {"type": "string"},
            "duration_minutes": {"type": "integer", "enum": [30, 60, 90]},
        },
        ("room", "date", "duration_minutes"),
    )
    geocod = _tool_decl(
        "geocode", "Resolve a city to coordinates", {"city": {"type": "string"}}, ("city",)
    )
    lectura = _tool_decl(
        "log_reading",
        "Append a sensor reading to the log",
        {"sensor": {"type": "string"}, "value": {"type": "number"}},
        ("sensor", "value"),
    )
    medidor = _tool_decl(
        "meter_read", "Read one meter", {"meter_id": {"type": "string"}}, ("meter_id",)
    )
    averia = _tool_decl(
        "report_outage",
        "File an outage report",
        {
            "zone": {"type": "string", "enum": ["north", "south", "east", "west"]},
            "affected": {"type": "integer", "minimum": 0},
        },
        ("zone", "affected"),
    )
    busqueda = _tool_decl(
        "parts_search",
        "Search the parts catalog",
        {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 9}},
        ("query", "limit"),
    )
    precio = _tool_decl(
        "price_check",
        "Price one part",
        {"part": {"type": "string"}, "currency": {"type": "string", "enum": ["USD", "EUR"]}},
        ("part", "currency"),
    )
    entrega = _tool_decl(
        "schedule_delivery",
        "Schedule a part delivery",
        {"part": {"type": "string"}, "days": {"type": "integer", "minimum": 1, "maximum": 30}},
        ("part", "days"),
    )
    return {
        "TR-1": {
            "task": "Check the current weather for Lisbon in celsius, then convert that "
            "temperature value to fahrenheit.",
            "sequence": ("weather_lookup", "unit_convert"),
            "tools": (clima, conversion),
        },
        "TR-2": {
            "task": "Look up part SKU BR-2214 in the west warehouse, then order a "
            "replacement, quantity 2.",
            "sequence": ("inventory_lookup", "order_replacement"),
            "tools": (inventario, repuesto),
        },
        "TR-3": {
            "task": "Reserve the aurora meeting room for 2026-09-03 for 90 minutes.",
            "sequence": ("room_reserve",),
            "tools": (sala,),
        },
        "TR-4": {
            "task": "Locate the city of Porto, check the weather there in celsius, then log "
            "the temperature on sensor OUT-7.",
            "sequence": ("geocode", "weather_lookup", "log_reading"),
            "tools": (geocod, clima, lectura),
        },
        "TR-5": {
            "task": "Read meter MTR-118, then report an outage for the north zone affecting "
            "12 customers.",
            "sequence": ("meter_read", "report_outage"),
            "tools": (medidor, averia),
        },
        "TR-6": {
            "task": "Search parts matching drive belt, price the first result in USD, then "
            "schedule its delivery in 3 days.",
            "sequence": ("parts_search", "price_check", "schedule_delivery"),
            "tools": (busqueda, precio, entrega),
        },
    }


_TOOL_SCENARIOS = _tool_scenarios()
_RE_SCENARIO = re.compile(r"Tool desk request (TR-\d+)\.")


def tool_scenario(prompt: str) -> dict:
    """The scenario a tool_calling prompt carries: id, tool declarations, sequence.

    Raises ValueError when the prompt names no known scenario: fixture drift is
    a harness bug, never a model verdict.
    """
    m = _RE_SCENARIO.search(prompt)
    tid = m.group(1) if m else None
    if tid not in _TOOL_SCENARIOS:
        raise ValueError("tool_calling prompt carries no known scenario id")
    return {
        "id": tid,
        "tools": _TOOL_SCENARIOS[tid]["tools"],
        "sequence": _TOOL_SCENARIOS[tid]["sequence"],
    }


def _tool_calling(_rng: random.Random) -> list[tuple[str, tuple[dict, ...]]]:
    return [
        (
            f"{_TOOL_DESK_HEADER.format(tid=tid)}\nTask: {_TOOL_SCENARIOS[tid]['task']}",
            _TOOL_SCENARIOS[tid]["tools"],
        )
        for tid in sorted(_TOOL_SCENARIOS)
    ]


# ---------------------------------------------------------------
# long_generation: one catalog assembled from a raw list — the checker
# grades the complete structure, not the prose.

LONG_GENERATION_SECTIONS = 25
LONG_GENERATION_ITEMS = 20
LONG_GENERATION_TAIL = ("end", "of", "generation")  # tokens ending the reply

_LONGGEN_HEADER = (
    "Build the parts catalog of the MK-4 line from the raw list below.\n"
    "Format rules:\n"
    f"- Exactly {LONG_GENERATION_SECTIONS} sections, in order. Each section starts with a "
    "header line: Section <n>: <short title>\n"
    f"- Exactly {LONG_GENERATION_ITEMS} items per section, in order. Each item is one line "
    "starting with: item <k>: <text>\n"
    "- Do not number anything else.\n"
    "- Finish with the single line: END OF GENERATION"
)
_LONGGEN_DEVICE_PREFIX = (
    ("valve", "VL"),
    ("sensor", "SN"),
    ("hose", "HS"),
    ("gasket", "GK"),
    ("motor", "MT"),
    ("filter", "FL"),
)


def _long_generation(rng: random.Random) -> list[tuple[str, tuple[dict, ...]]]:
    entradas = []
    for i in range(1, 61):
        _device, prefix = _LONGGEN_DEVICE_PREFIX[i % len(_LONGGEN_DEVICE_PREFIX)]
        entradas.append(
            f"raw {prefix}-{rng.randrange(100, 1000)} bay {rng.randrange(1, 41)} "
            f"torque {rng.randrange(10, 90)} Nm {rng.choice(_PAD_SENTENCES)}"
        )
    return [(f"{_LONGGEN_HEADER}\n\n" + "\n".join(entradas), ())]


# ---------------------------------------------------------------
# reasoning: a small arithmetic chain buried in protocol padding

REASONING_OPENING = "Bay audit. Work from the rules below only."


def _pad_paragraph(rng: random.Random, sentences: int) -> str:
    """Padding with no digits and no 'answer': it can never fake a datum."""
    return " ".join(rng.choice(_PAD_SENTENCES) for _ in range(sentences))


def _reasoning(rng: random.Random) -> list[tuple[str, tuple[dict, ...]]]:
    filas = rng.randrange(5, 41)
    columnas = rng.randrange(3, 13)
    reservadas = rng.randrange(1, 100)
    texto = (
        f"{REASONING_OPENING}\n"
        f"Rows: {filas}\n"
        f"Columns: {columnas}\n"
        f"Reserved bays: {reservadas}\n"
        "Rule: the bay access number equals the number of rows times the number of "
        "columns plus the number of reserved bays.\n\n"
        + "\n\n".join(_pad_paragraph(rng, 30) for _ in range(3))
        + "\n\nWhat is the bay access number? End your reply with a line of the form: "
        "ANSWER: <number>"
    )
    return [(texto, ())]


def reasoning_expected(prompt: str) -> int:
    """The bay access number the prompt's rules determine (checker + test helper)."""
    filas = re.search(r"^Rows: (\d+)$", prompt, re.MULTILINE)
    columnas = re.search(r"^Columns: (\d+)$", prompt, re.MULTILINE)
    reservadas = re.search(r"^Reserved bays: (\d+)$", prompt, re.MULTILINE)
    if not (filas and columnas and reservadas):
        raise ValueError("reasoning prompt does not carry the three rule lines")
    return int(filas.group(1)) * int(columnas.group(1)) + int(reservadas.group(1))


# ---------------------------------------------------------------
# ratio_out: 20 short-input generations of ~500 output tokens, graded on
# the complete structure (10 numbered notes + the final word).

RATIO_OUT_NOTES = 10
RATIO_OUT_TAIL = "endcap"  # the final token the contract demands

_RATIO_OUT_PROMPT = (
    "Write the shift handover note about {topic}.\n"
    "Format rules:\n"
    "- Exactly 10 lines, in order. Each line starts: Note <n>: (n from 1 to 10)\n"
    "- Each note line is one complete sentence.\n"
    "- The final line ends with the single word: ENDCAP"
)
_RATIO_OUT_TOPICS = (
    "the coolant loop",
    "the air handling unit",
    "the kiln burners",
    "the packaging line",
    "the compressor unloader",
    "the boiler blowdown",
    "the cooling tower fill",
    "the conveyor drive",
    "the crusher jaw plates",
    "the paint booth filters",
    "the spare parts annex",
    "the interlock test routine",
    "the lubrication chart",
    "the bypass valve",
    "the water treatment skid",
    "the dust extraction hoods",
    "the loading dock",
    "the tool crib inventory",
    "the night journal",
    "the annex stairs",
    "the frame anchors",
    "the temperature log",
    "the filter stock",
    "the office chart",
)


def _ratio_out(rng: random.Random) -> list[tuple[str, tuple[dict, ...]]]:
    temas = rng.sample(list(_RATIO_OUT_TOPICS), 20)
    return [(f"{_RATIO_OUT_PROMPT.format(topic=t)} Answer with the note only.", ()) for t in temas]


# ---------------------------------------------------------------
# cache calibration (methodology v1 §7): the fixed ~20K prefix shared with
# long_context — the register's opening span, truncated before its task block —
# re-sent r=4 intra-batch and re-sent again in spaced batches (5/30/90 s). The
# three phase workloads serve the identical prompt (one prefix, one provenance);
# the phase lives in the workload name so the raw dataset is self-describing.

CACHE_PREFIX_LINES = 550  # the long_context document's opening span (~20K tokens)
CACHE_N_BY_WORKLOAD = {"cache_cold": 1, "cache_intra": 4, "cache_spaced": 3}

_CACHE_HEADER = (
    "Cache calibration replay. The register below is the fixed measurement "
    "prefix; read it and answer the final instruction only."
)
_CACHE_TASK = "Reply with the single word: OK."


def cache_prefix_prompt() -> str:
    """The calibration's fixed prefix: the long_context register's opening span
    (truncated before its task block) under the calibration's own header.

    The bytes are the long_context fixture's own — same generator, same seed
    lineage — so the measured prefix and the T2 suite share one provenance; the
    header and the one-word task keep the replay a workload of its own (and
    `workload_of` unambiguous).
    """
    largo, _ = _long_context(_rng("long_context"))[0]
    cuerpo = largo[: largo.index(f"\n\n{_REGISTER_TASK}")]  # register only: no task block
    lineas = cuerpo.splitlines()[2:]  # drop the register header + blank: the replay carries its own
    return f"{_CACHE_HEADER}\n\n" + "\n".join(lineas[:CACHE_PREFIX_LINES]) + f"\n\n{_CACHE_TASK}"


# ---------------------------------------------------------------
# Dispatch

_GENERATORS = {
    "long_context": _long_context,
    "long_generation": _long_generation,
    "multi_turn": _multi_turn,
    "tool_calling": _tool_calling,
    "reasoning": _reasoning,
    "ratio_in": _ratio_in,
    "ratio_out": _ratio_out,
}

# Unique opening line of every T2 prompt (workload_of + test dispatch rely on it).
WORKLOAD_OPENINGS: dict[str, str] = {
    "long_context": _REGISTER_HEADER,
    "long_generation": _LONGGEN_HEADER.splitlines()[0],
    "multi_turn": _MULTI_TURN_HEADER,
    "reasoning": REASONING_OPENING,
    "ratio_in": _REGISTER_HEADER,
    "ratio_out": "Write the shift handover note about ",
    "tool_calling": "Tool desk request TR-",
}


def workload_of(prompt: str) -> str:
    """Which T2 workload generated this prompt (fixtures open with unique lines)."""
    coincidencias = [w for w, opening in WORKLOAD_OPENINGS.items() if prompt.startswith(opening)]
    if len(coincidencias) == 1:
        return coincidencias[0]
    if len(coincidencias) == 2:  # the two register workloads share the header
        return "ratio_in" if prompt.rstrip().endswith(_REGISTER_REPLY_ONLY) else "long_context"
    raise ValueError(f"prompt carries no T2 fixture opening: {prompt[:60]!r}")


def specs(workload: str, n: int) -> list[tuple[str, tuple[dict, ...]]]:
    """The workload's `n` deterministic (prompt, tools) pairs.

    Raises ValueError on an unknown workload or a wrong request count: the
    workload table's shape is part of the fixture contract. The cache
    calibration's phase workloads (cache_cold/cache_intra/cache_spaced) serve
    the one fixed prefix with their own pinned request counts.
    """
    if workload in CACHE_N_BY_WORKLOAD:
        if n != CACHE_N_BY_WORKLOAD[workload]:
            raise ValueError(
                f"cache workload {workload!r} runs {CACHE_N_BY_WORKLOAD[workload]} "
                f"request(s) per bracket, not {n}"
            )
        return [(cache_prefix_prompt(), ())] * n
    if workload not in _N_BY_WORKLOAD:
        raise ValueError(f"unknown T2 workload: {workload!r}")
    if n != _N_BY_WORKLOAD[workload]:
        raise ValueError(
            f"T2 workload {workload!r} runs {_N_BY_WORKLOAD[workload]} request(s) per "
            f"repetition, not {n}"
        )
    return list(_GENERATORS[workload](_rng(workload)))
