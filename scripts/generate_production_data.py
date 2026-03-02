#!/usr/bin/env python3
"""
Generate comprehensive production data JSON for NAIS (North American Industry Set).

Programmatically parses the NAIS source directory to extract all industry and cargo
definitions, then computes production tables.  Validates the result against any
existing (legacy) JSON and reports differences.

Usage:
    python scripts/generate_production_data.py            # generate + validate
    python scripts/generate_production_data.py --skip-validate   # generate only
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

NAIS_ROOT = PROJECT_ROOT / "nais" / "NAIS - NORTH AMERICAN INDUSTRY SET"
NAIS_SRC = NAIS_ROOT / "src"
INDUSTRIES_DIR = NAIS_SRC / "industries"
CARGOS_DIR = NAIS_SRC / "cargos"
LANG_FILE = NAIS_SRC / "lang" / "english.lng"
MAKEFILE = NAIS_ROOT / "Makefile"

JSON_PATH = DATA_DIR / "nais_production_data.json"

# ---------------------------------------------------------------------------
# Production-mechanic constants — parsed from NAIS source files
# ---------------------------------------------------------------------------
def _parse_random_factors(path: Path) -> list[tuple[int, int]]:
    """Parse ``randomise_primary_production_on_build.pynml`` for (value, weight) pairs."""
    src = path.read_text(encoding="utf-8")
    return [(int(v), int(w)) for w, v in re.findall(r"(\d+):\s*return\s+(\d+);", src)]


def _parse_global_constant(path: Path, name: str) -> int:
    """Extract a named integer constant from ``global_constants.py``."""
    src = path.read_text(encoding="utf-8")
    m = re.search(rf"^{name}\s*=\s*(\d+)", src, re.MULTILINE)
    if not m:
        raise ValueError(f"Cannot find {name} in {path}")
    return int(m.group(1))


def _parse_header_params(path: Path) -> dict[str, int]:
    """Extract ``def_value`` for named parameters from ``header.pynml``.

    Returns a dict like ``{"primary_level1_produced_percent": 150, ...}``.
    Only captures simple integer ``def_value`` lines (ignores template expressions).
    """
    src = path.read_text(encoding="utf-8")
    params: dict[str, int] = {}
    # Match: "param N { name { ... def_value: INT; ..."
    for m in re.finditer(
        r"param\s+\d+\s*\{\s*(\w+)\s*\{[^}]*?def_value:\s*(\d+)\s*;",
        src, re.DOTALL,
    ):
        params[m.group(1)] = int(m.group(2))
    return params


TEMPLATES_DIR = NAIS_SRC / "templates"

RANDOM_FACTORS = _parse_random_factors(TEMPLATES_DIR / "randomise_primary_production_on_build.pynml")
TOTAL_WEIGHT = sum(w for _, w in RANDOM_FACTORS)
WEIGHTED_AVG_FACTOR = sum(f * w for f, w in RANDOM_FACTORS) / TOTAL_WEIGHT

_FARM_MINE_REQ = _parse_global_constant(NAIS_SRC / "global_constants.py", "FARM_MINE_SUPPLY_REQUIREMENT")
_header_params = _parse_header_params(TEMPLATES_DIR / "header.pynml")

LEVEL1_REQUIREMENT = _FARM_MINE_REQ
LEVEL2_REQUIREMENT = 5 * _FARM_MINE_REQ  # multiplier from header.pynml L2 def_value expression
LEVEL1_PERCENT = _header_params["primary_level1_produced_percent"]
LEVEL2_PERCENT = _header_params["primary_level2_produced_percent"]

# ---------------------------------------------------------------------------
# Parse base-class accept_cargo_types and supply_requirements from industry.py
# ---------------------------------------------------------------------------
def _parse_base_class_info(industry_py: Path) -> tuple[
    dict[str, list[str] | None],   # class_name → accept_cargo_types or None
    dict[str, int | None],         # class_name → supply multiplier or None
]:
    """Parse NAIS ``industry.py`` for per-class accept_cargo_types and supply multipliers.

    Extracts from each class ``__init__``:
      - ``kwargs['accept_cargo_types'] = [...]`` → list of cargo labels
      - ``self.supply_requirements = [0, 'PREFIX', multiplier]`` → int multiplier
      - ``self.supply_requirements = None`` → None
    """
    src = industry_py.read_text(encoding="utf-8")

    # Split into class blocks: find each "class ClassName(...)" and its body
    class_pattern = re.compile(
        r"^class\s+(\w+)\s*\([^)]*\)\s*:",
        re.MULTILINE,
    )
    class_starts = [(m.start(), m.group(1)) for m in class_pattern.finditer(src)]

    accepts: dict[str, list[str] | None] = {}
    supply_mult: dict[str, int | None] = {}

    for i, (start, cls_name) in enumerate(class_starts):
        end = class_starts[i + 1][0] if i + 1 < len(class_starts) else len(src)
        block = src[start:end]

        # accept_cargo_types set in __init__ via kwargs
        act_m = re.search(
            r"kwargs\s*\[\s*['\"]accept_cargo_types['\"]\s*\]\s*=\s*\[([^\]]*)\]",
            block,
        )
        if act_m:
            accepts[cls_name] = re.findall(r"'(\w+)'", act_m.group(1))
        else:
            accepts[cls_name] = None  # uses kwargs directly or no accept

        # supply_requirements
        sr_m = re.search(
            r"self\.supply_requirements\s*=\s*(\[.*?\]|None)",
            block,
        )
        if sr_m:
            raw = sr_m.group(1)
            if raw == "None":
                supply_mult[cls_name] = None
            else:
                # Parse [0, 'PREFIX', multiplier]
                nums = re.findall(r"\d+", raw)
                supply_mult[cls_name] = int(nums[-1]) if nums else None
        # If no supply_requirements line, leave it out (non-primary classes)

    return accepts, supply_mult


INDUSTRY_PY = NAIS_SRC / "industry.py"
BASE_CLASS_ACCEPTS, SUPPLY_MULTIPLIER = _parse_base_class_info(INDUSTRY_PY)

# Editorial enrichments for cargo names (parenthetical notes not in the lang file)
CARGO_NAME_OVERRIDES: dict[str, str] = {
    "ENSP": "Machinery (Engineering Supplies)",
    "FMSP": "Fertilizer (Farm Supplies)",
    "FRUT": "Produce (Fruit)",
    "GRVL": "Stone (Gravel)",
    "PETR": "Fuel (Petroleum)",
    "RFPR": "Chemicals (Refined Products)",
    "WDPR": "Lumber (Wood Products)",
    "FICR": "Fiber Crops",  # lang file uses British "Fibre"
}


# ===================================================================
# Source-file parsers
# ===================================================================

def parse_nais_version() -> str:
    """Extract REPO_VERSION from the NAIS Makefile.

    Matches the hardcoded version (e.g. ``1.0.6``) rather than the
    Makefile variable expression ``$(word ...)``.
    """
    if not MAKEFILE.exists():
        return "unknown"
    content = MAKEFILE.read_text(encoding="utf-8")
    m = re.search(r"REPO_VERSION\s*=\s*(\d+\.\d+\.\d+)", content)
    return m.group(1) if m else "unknown"


def parse_lang_file() -> dict[str, str]:
    """Parse ``english.lng`` → {STR_KEY: "Human Name"}."""
    mapping: dict[str, str] = {}
    with open(LANG_FILE, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            mapping[key.strip()] = value.strip()
    return mapping


def parse_cargo_files(
    lang_strings: dict[str, str],
) -> tuple[dict[str, str], dict[str, dict]]:
    """Parse every ``cargos/*.py`` → cargo names and properties.

    Returns ``(cargo_names, cargo_props)`` where:
      - ``cargo_names``: ``{cargo_label: "Human Name"}``
      - ``cargo_props``: ``{cargo_label: {price_factor, penalty_lowerbound,
        single_penalty_length, cargo_classes, is_freight, weight,
        town_growth_effect, capacity_multiplier}}``

    Uses the ``type_name`` field to look up the lang string, then applies
    editorial overrides for enriched names.
    """
    cargo_names: dict[str, str] = {}
    cargo_props: dict[str, dict] = {}
    for py_file in sorted(CARGOS_DIR.glob("*.py")):
        if py_file.name == "__init__.py":
            continue
        content = py_file.read_text(encoding="utf-8")

        label_m = re.search(r"cargo_label\s*=\s*'(\w+)'", content)
        type_m = re.search(r"type_name\s*=\s*'string\((\w+)\)'", content)
        if not label_m or not type_m:
            continue

        label = label_m.group(1)
        str_key = type_m.group(1)
        name = lang_strings.get(str_key, label)

        # Apply editorial overrides
        cargo_names[label] = CARGO_NAME_OVERRIDES.get(label, name)

        # Extract payment properties
        pf_m = re.search(r"price_factor\s*=\s*'(\d+)'", content)
        plb_m = re.search(r"penalty_lowerbound\s*=\s*'(\d+)'", content)
        spl_m = re.search(r"single_penalty_length\s*=\s*'(\d+)'", content)
        props: dict = {}
        if pf_m:
            props["price_factor"] = int(pf_m.group(1))
        if plb_m:
            props["penalty_lowerbound"] = int(plb_m.group(1))
        if spl_m:
            props["single_penalty_length"] = int(spl_m.group(1))

        # Extract cargo classes (list of CC_* identifiers)
        cc_m = re.search(r"cargo_classes\s*=\s*'bitmask\(([^)]+)\)'", content)
        if cc_m:
            classes = [c.strip() for c in cc_m.group(1).split(",")]
            props["cargo_classes"] = classes

        # Extract is_freight flag
        if_m = re.search(r"is_freight\s*=\s*'([01])'", content)
        if if_m:
            props["is_freight"] = bool(int(if_m.group(1)))

        # Extract weight per unit
        wt_m = re.search(r"weight\s*=\s*'([\d.]+)'", content)
        if wt_m:
            props["weight"] = float(wt_m.group(1))

        # Extract town growth effect
        tge_m = re.search(r"town_growth_effect\s*=\s*'(TOWNGROWTH_\w+)'", content)
        if tge_m:
            props["town_growth_effect"] = tge_m.group(1)

        # Extract capacity multiplier
        cm_m = re.search(r"capacity_multiplier\s*=\s*'([\d.]+)'", content)
        if cm_m:
            props["capacity_multiplier"] = float(cm_m.group(1))

        if props:
            cargo_props[label] = props

    return cargo_names, cargo_props


def parse_industry_file(path: Path) -> dict | None:
    """Extract structured data from a single ``industries/*.py`` file.

    Returns *None* if the industry is not enabled for NORTH_AMERICA.
    """
    content = path.read_text(encoding="utf-8")

    # Must be enabled for NORTH_AMERICA
    if "economy_variations['NORTH_AMERICA'].enabled = True" not in content:
        return None

    # Industry type (first import from industry module)
    type_m = re.search(r"from industry import (\w+)", content)
    if not type_m:
        return None
    industry_type: str = type_m.group(1)

    # processed_cargos_and_output_ratios  (secondary / ranch-with-secondary-like inputs)
    pcaor: list[tuple[str, int]] | None = None
    pcaor_m = re.search(
        r"processed_cargos_and_output_ratios\s*[=\t ]+\[([^\]]+)\]", content
    )
    if pcaor_m:
        pcaor = [
            (lbl, int(r))
            for lbl, r in re.findall(r"\('(\w+)',\s*(\d+)\)", pcaor_m.group(1))
        ]

    # prod_cargo_types — can be plain strings or 2-tuples
    prod_labels: list[str] = []
    prod_tuples: list[tuple[str, int]] = []
    pct_m = re.search(r"prod_cargo_types\s*[=\t ]+\[([^\]]*)\]", content)
    if pct_m:
        raw = pct_m.group(1)
        tuples_found = re.findall(r"\('(\w+)',\s*(\d+)\)", raw)
        if tuples_found:
            prod_tuples = [(lbl, int(r)) for lbl, r in tuples_found]
        tuple_labels = {t[0] for t in prod_tuples}
        prod_labels = [s for s in re.findall(r"'(\w+)'", raw) if s not in tuple_labels]

    # accept_cargo_types (explicit in file — used by ports, tertiary, etc.)
    accept: list[str] | None = None
    act_m = re.search(r"accept_cargo_types\s*[=\t ]+\[([^\]]*)\]", content)
    if act_m:
        accept = re.findall(r"'(\w+)'", act_m.group(1))

    # prod_multiplier
    prod_mult: list[int] | None = None
    pm_m = re.search(r"prod_multiplier\s*[=\t ]+'?\[([^\]]*)\]'?", content)
    if pm_m:
        prod_mult = [int(x.strip()) for x in pm_m.group(1).split(",") if x.strip()]

    # combined_cargos_boost_prod
    combined: bool | None = None
    ccbp_m = re.search(r"combined_cargos_boost_prod\s*[=\t ]+(True|False)", content)
    if ccbp_m:
        combined = ccbp_m.group(1) == "True"

    return {
        "type": industry_type,
        "pcaor": pcaor,
        "prod_labels": prod_labels,
        "prod_tuples": prod_tuples,
        "accept": accept,
        "prod_mult": prod_mult,
        "combined": combined,
    }


# ===================================================================
# Production computation helpers
# ===================================================================

def prod_range(multiplier: int) -> dict:
    """Calculate production range for a given ``prod_multiplier``."""
    min_f = min(f for f, _ in RANDOM_FACTORS)
    max_f = max(f for f, _ in RANDOM_FACTORS)
    return {
        "min_base": multiplier * min_f,
        "max_base": multiplier * max_f,
        "weighted_average_base": round(multiplier * WEIGHTED_AVG_FACTOR),
        "with_level1_supplies_min": round(multiplier * min_f * LEVEL1_PERCENT / 100),
        "with_level1_supplies_max": round(multiplier * max_f * LEVEL1_PERCENT / 100),
        "with_level2_supplies_min": round(multiplier * min_f * LEVEL2_PERCENT / 100),
        "with_level2_supplies_max": round(multiplier * max_f * LEVEL2_PERCENT / 100),
    }


def calc_secondary_output(input_amount: int, effective_ratio: int, output_ratio: int) -> int:
    """Calculate secondary industry output using integer division (as in NML)."""
    return input_amount * effective_ratio * output_ratio // 64


def make_secondary_production_table(
    input_cargos: list[tuple[str, int]],
    output_cargos: list[tuple[str, int]],
    combined_boost: bool,
) -> dict:
    """Generate production tables for all combinations of input cargo delivery.

    Returns ``{scenario_key: {output_key: amount_per_8_input}}``.
    """
    n = len(input_cargos)
    scenarios: dict[str, dict[str, int]] = {}

    for mask in range(1, 1 << n):
        delivered = [i for i in range(n) if mask & (1 << i)]
        scenario_key = "+".join(input_cargos[i][0] for i in delivered)

        scenario_data: dict[str, int] = {}
        for i in delivered:
            cargo_label = input_cargos[i][0]
            base_ratio = input_cargos[i][1]

            if combined_boost:
                boost = sum(input_cargos[j][1] for j in delivered if j != i)
                effective_ratio = base_ratio + boost
            else:
                effective_ratio = base_ratio

            for out_label, out_ratio in output_cargos:
                output_per_8 = calc_secondary_output(8, effective_ratio, out_ratio)
                key = f"{out_label}_per_8_{cargo_label}"
                scenario_data[key] = output_per_8

        scenarios[scenario_key] = scenario_data

    return scenarios


def resolve_output_cargos(
    prod_labels: list[str],
    prod_tuples: list[tuple[str, int]],
) -> list[tuple[str, int]]:
    """Resolve output cargos from parsed ``prod_cargo_types``.

    Mirrors the logic in ``Industry.get_prod_cargo_types()``:
    - plain strings → 4 each if 2 outputs, 8 if 1 output
    - explicit tuples → use as-is
    """
    result: list[tuple[str, int]] = list(prod_tuples)
    total_items = len(prod_labels) + len(prod_tuples)
    for lbl in prod_labels:
        if total_items == 2:
            result.append((lbl, 4))
        else:
            result.append((lbl, 8))
    return result


# ===================================================================
# Industry builders
# ===================================================================

def build_primary_industry(
    name: str,
    src: dict,
    cargo_names: dict[str, str],
) -> dict:
    """Build a primary-industry JSON entry from parsed source data."""
    industry_type: str = src["type"]

    # --- Determine accepted cargos ---
    base_accepts = BASE_CLASS_ACCEPTS.get(industry_type)
    if base_accepts is not None:
        # Base class overrides accept_cargo_types
        accepts = base_accepts
    elif src["accept"] is not None:
        accepts = src["accept"]
    else:
        accepts = []

    # --- Determine produced cargos + multipliers ---
    prod_labels = src["prod_labels"]
    prod_mult = src["prod_mult"] or []
    # Filter out trailing zeros from prod_multiplier
    prod_mult_nonzero = [x for x in prod_mult if x > 0]

    produces: dict[str, dict] = {}
    for idx, label in enumerate(prod_labels):
        mult = prod_mult_nonzero[idx] if idx < len(prod_mult_nonzero) else 0
        produces[label] = {
            "cargo_name": cargo_names.get(label, label),
            "prod_multiplier": mult,
            "production_range": prod_range(mult),
        }

    # --- Supply requirements ---
    supply_mult = SUPPLY_MULTIPLIER.get(industry_type)
    if supply_mult is not None:
        supply_req: dict | None = {
            "level1_threshold_3months": LEVEL1_REQUIREMENT * supply_mult,
            "level1_production_percent": LEVEL1_PERCENT,
            "level2_threshold_3months": LEVEL2_REQUIREMENT * supply_mult,
            "level2_production_percent": LEVEL2_PERCENT,
        }
        if supply_mult == 8:
            supply_req["note"] = "Port type uses 8x the base supply requirement"
    else:
        supply_req = None

    entry: dict = {
        "type": industry_type,
        "accepts": [
            {"label": lbl, "name": cargo_names.get(lbl, lbl), "purpose": "boost production"}
            for lbl in accepts
        ],
        "produces": produces,
        "supply_requirements": supply_req,
    }

    if industry_type == "IndustryPrimaryNoSupplies":
        entry["supply_requirements"] = None
        entry["note"] = "No supply boost available. Production is fixed at initial random level."

    return entry


def build_secondary_industry(
    name: str,
    src: dict,
    cargo_names: dict[str, str],
) -> dict:
    """Build a secondary-industry JSON entry from parsed source data."""
    pcaor = src["pcaor"] or []
    combined = src["combined"] if src["combined"] is not None else False

    # Input cargos from processed_cargos_and_output_ratios
    input_cargos_info = [
        {"label": lbl, "name": cargo_names.get(lbl, lbl), "base_ratio": ratio}
        for lbl, ratio in pcaor
    ]

    # Output cargos
    output_tuples = resolve_output_cargos(src["prod_labels"], src["prod_tuples"])
    output_cargos_info = [
        {"label": lbl, "name": cargo_names.get(lbl, lbl), "output_ratio": ratio}
        for lbl, ratio in output_tuples
    ]

    # Production table
    prod_table = make_secondary_production_table(pcaor, output_tuples, combined)

    return {
        "type": "IndustrySecondary",
        "combined_cargos_boost_prod": combined,
        "input_cargos": input_cargos_info,
        "output_cargos": output_cargos_info,
        "production_per_8_units_input": prod_table,
    }


def build_tertiary_industry(
    name: str,
    src: dict,
    cargo_names: dict[str, str],
) -> dict:
    """Build a tertiary-industry JSON entry from parsed source data."""
    accepts = src["accept"] or []
    prod_labels = src["prod_labels"]
    prod_mult = src["prod_mult"] or []
    prod_mult_nonzero = [x for x in prod_mult if x > 0]

    produces: dict[str, dict] = {}
    for idx, label in enumerate(prod_labels):
        mult = prod_mult_nonzero[idx] if idx < len(prod_mult_nonzero) else 0
        produces[label] = {
            "cargo_name": cargo_names.get(label, label),
            "prod_multiplier": mult,
            "note": "Tertiary production is typically fixed/town-based",
        }

    return {
        "type": "IndustryTertiary",
        "accepts": [{"label": lbl, "name": cargo_names.get(lbl, lbl)} for lbl in accepts],
        "produces": produces if produces else "none (pure consumer)",
    }


# ===================================================================
# Classification
# ===================================================================

PRIMARY_TYPES = {
    "IndustryPrimaryExtractive",
    "IndustryPrimaryOrganic",
    "IndustryPrimaryRanch",
    "IndustryPrimaryPort",
    "IndustryPrimaryNoSupplies",
    "IndustryPrimaryTownProducer",
}
SECONDARY_TYPES = {"IndustrySecondary"}
TERTIARY_TYPES = {"IndustryTertiary", "IndustryBank"}


# ===================================================================
# Validation against legacy JSON
# ===================================================================

def validate_against_legacy(new_data: dict, legacy_path: Path) -> list[str]:
    """Compare *new_data* against the legacy JSON at *legacy_path*.

    Returns a list of human-readable difference strings (empty = perfect match).
    """
    if not legacy_path.exists():
        return ["Legacy JSON not found — skipping validation."]

    with open(legacy_path, encoding="utf-8") as fh:
        legacy = json.load(fh)

    diffs: list[str] = []

    # --- Cargo definitions ---
    new_cargos = new_data.get("cargo_definitions", {})
    old_cargos = legacy.get("cargo_definitions", {})
    for label in sorted(set(new_cargos) | set(old_cargos)):
        if label not in new_cargos:
            diffs.append(f"cargo {label}: MISSING in new (present in legacy)")
        elif label not in old_cargos:
            diffs.append(f"cargo {label}: NEW (not in legacy)")
        elif new_cargos[label] != old_cargos[label]:
            diffs.append(f"cargo {label}: name differs — new={new_cargos[label]} legacy={old_cargos[label]}")

    # --- Industries ---
    for category in ("primary", "secondary", "tertiary"):
        new_inds = new_data.get("industries", {}).get(category, {})
        old_inds = legacy.get("industries", {}).get(category, {})
        all_names = sorted(set(new_inds) | set(old_inds))
        for ind_name in all_names:
            prefix = f"{category}/{ind_name}"
            if ind_name not in new_inds:
                diffs.append(f"{prefix}: MISSING in new")
                continue
            if ind_name not in old_inds:
                diffs.append(f"{prefix}: NEW (not in legacy)")
                continue

            # Deep compare
            _deep_diff(new_inds[ind_name], old_inds[ind_name], prefix, diffs)

    return diffs


def _deep_diff(new: object, old: object, path: str, diffs: list[str]) -> None:
    """Recursively compare two JSON-like objects."""
    if type(new) != type(old):
        diffs.append(f"{path}: type mismatch new={type(new).__name__} legacy={type(old).__name__}")
        return
    if isinstance(new, dict):
        for key in sorted(set(new) | set(old)):  # type: ignore[arg-type]
            if key not in new:  # type: ignore[operator]
                diffs.append(f"{path}.{key}: MISSING in new")
            elif key not in old:  # type: ignore[operator]
                diffs.append(f"{path}.{key}: NEW field")
            else:
                _deep_diff(new[key], old[key], f"{path}.{key}", diffs)  # type: ignore[index]
    elif isinstance(new, list):
        if len(new) != len(old):  # type: ignore[arg-type]
            diffs.append(f"{path}: list length differs new={len(new)} legacy={len(old)}")  # type: ignore[arg-type]
        for i, (a, b) in enumerate(zip(new, old)):  # type: ignore[arg-type]
            _deep_diff(a, b, f"{path}[{i}]", diffs)
    else:
        if new != old:
            diffs.append(f"{path}: value differs new={new!r} legacy={old!r}")


# ===================================================================
# Main
# ===================================================================

def generate() -> dict:
    """Parse NAIS source and return the complete production-data dict."""
    # 0. Extract version
    nais_version = parse_nais_version()

    # 1. Parse lang + cargo files
    lang_strings = parse_lang_file()
    cargo_names, cargo_props = parse_cargo_files(lang_strings)

    # 2. Parse all industry files
    primary_industries: dict[str, dict] = {}
    secondary_industries: dict[str, dict] = {}
    tertiary_industries: dict[str, dict] = {}

    for py_file in sorted(INDUSTRIES_DIR.glob("*.py")):
        if py_file.name == "__init__.py":
            continue
        name = py_file.stem
        src = parse_industry_file(py_file)
        if src is None:
            continue  # not enabled for NORTH_AMERICA

        itype = src["type"]
        if itype in PRIMARY_TYPES:
            primary_industries[name] = build_primary_industry(name, src, cargo_names)
        elif itype in SECONDARY_TYPES:
            secondary_industries[name] = build_secondary_industry(name, src, cargo_names)
        elif itype in TERTIARY_TYPES:
            tertiary_industries[name] = build_tertiary_industry(name, src, cargo_names)
        else:
            print(f"  ⚠ Unknown industry type {itype!r} for {name}, skipping")

    # 3. Assemble final structure
    output = {
        "_metadata": {
            "title": "NAIS - North American Industry Set: Complete Production Data",
            "nais_version": nais_version,
            "source": "Extracted from NAIS source code (FIRS-based OpenTTD NewGRF)",
            "production_mechanics": {
                "primary_industries": {
                    "base_production_formula": (
                        "prod_multiplier × random_factor "
                        "(per production cycle of ~256 ticks)"
                    ),
                    "random_factor_on_build": {
                        "values_and_weights": [
                            {
                                "factor": f,
                                "weight": w,
                                "probability": f"{w}/{TOTAL_WEIGHT}",
                            }
                            for f, w in RANDOM_FACTORS
                        ],
                        "weighted_average": round(WEIGHTED_AVG_FACTOR, 1),
                    },
                    "supply_boost_levels": {
                        "no_supplies": "100% of base production",
                        "level1": {
                            "requirement": (
                                f">= {LEVEL1_REQUIREMENT} units of supplies "
                                "delivered over 3 months"
                            ),
                            "production": f"{LEVEL1_PERCENT}% of base production",
                        },
                        "level2": {
                            "requirement": (
                                f">= {LEVEL2_REQUIREMENT} units of supplies "
                                "delivered over 3 months"
                            ),
                            "production": f"{LEVEL2_PERCENT}% of base production",
                        },
                        "note": (
                            "These are default parameter values; "
                            "players can configure them."
                        ),
                    },
                    "port_industries_note": (
                        "Port industries use 8× the base supply "
                        "requirement thresholds."
                    ),
                },
                "secondary_industries": {
                    "formula": (
                        "For each input cargo i: output_j += "
                        "floor(input_amount_i × effective_ratio_i "
                        "× output_ratio_j / 64)"
                    ),
                    "divisor_explanation": (
                        "64 = input_ratio_max_sum(8) × output_ratio_max_sum(8)"
                    ),
                    "combinatory_boost": {
                        "description": (
                            "When combined_cargos_boost_prod=true, delivering "
                            "multiple cargos within 90 days boosts each cargo's "
                            "effective ratio."
                        ),
                        "formula": (
                            "effective_ratio_i = base_ratio_i + "
                            "sum(base_ratio_k for each OTHER cargo k "
                            "delivered within 90 days)"
                        ),
                        "example": (
                            "If cargo A has ratio 3 and cargo B has ratio 5, "
                            "delivering both gives A effective ratio 3+5=8 "
                            "and B effective ratio 5+3=8"
                        ),
                    },
                    "non_combinatory": (
                        "When combined_cargos_boost_prod=false, effective_ratio "
                        "always equals base_ratio regardless of other deliveries."
                    ),
                    "delivery_window": (
                        "90 days - cargos must be delivered within this window "
                        "to count as concurrent for combinatory boost."
                    ),
                },
                "tertiary_industries": {
                    "description": (
                        "Consumer industries that accept cargos but produce "
                        "little or nothing. They serve as demand sinks."
                    ),
                },
            },
        },
        "cargo_definitions": {
            label: {"name": name, **cargo_props.get(label, {})}
            for label, name in sorted(cargo_names.items())
        },
        "industries": {
            "primary": primary_industries,
            "secondary": secondary_industries,
            "tertiary": tertiary_industries,
        },
    }

    return output


def main() -> None:
    skip_validate = "--skip-validate" in sys.argv

    print("Parsing NAIS source directory …")
    data = generate()

    p = data["industries"]["primary"]
    s = data["industries"]["secondary"]
    t = data["industries"]["tertiary"]
    c = data["cargo_definitions"]
    print(f"  {len(p)} primary industries")
    print(f"  {len(s)} secondary industries")
    print(f"  {len(t)} tertiary industries")
    print(f"  {len(c)} cargo types")

    # --- Validate against legacy JSON (before overwriting) ---
    if not skip_validate and JSON_PATH.exists():
        print("\nValidating against legacy JSON …")
        diffs = validate_against_legacy(data, JSON_PATH)
        if diffs:
            print(f"  ⚠ {len(diffs)} difference(s) found:")
            for d in diffs:
                print(f"    • {d}")
        else:
            print("  ✅ Perfect match with legacy JSON!")

    # --- Write output ---
    with open(JSON_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    print(f"\nWrote {JSON_PATH}")


if __name__ == "__main__":
    main()
