#!/usr/bin/env python3
"""
Plotly figure builders for NAIS (North American Industry Set) industry & cargo views.

Four figure builders:
  1. build_sankey_figure()   — full cargo flow network (returns (fig, sankey_meta) tuple)
  2. build_primary_figure()  — box plots of production ranges with supply boost levels
  3. build_heatmap_figure()  — secondary industry conversion heatmap
  4. build_combo_figure()    — solo vs combined delivery comparison

Invoked by plot_dashboard.py to assemble the unified dashboard.

Reads: data/nais_production_data.json
"""

import json
import os

import plotly.graph_objects as go
import plotly.express as px

# ── Paths ────────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

JSON_PATH = os.path.join(DATA_DIR, "nais_production_data.json")

# ── Load data ────────────────────────────────────────────────────────────
with open(JSON_PATH) as f:
    DATA = json.load(f)

PRIMARY = DATA["industries"]["primary"]
SECONDARY = DATA["industries"]["secondary"]
TERTIARY = DATA["industries"]["tertiary"]
CARGO_DEFS = DATA["cargo_definitions"]


def pretty(name: str) -> str:
    """Convert snake_case industry name to Title Case."""
    return name.replace("_", " ").title()


# ── Consistent cargo color palette ──────────────────────────────────────
ALL_CARGO_LABELS = sorted(CARGO_DEFS.keys())
_palette = px.colors.qualitative.Dark24 + px.colors.qualitative.Light24
CARGO_COLORS = {label: _palette[i % len(_palette)] for i, label in enumerate(ALL_CARGO_LABELS)}

# Industry-type colors
TYPE_COLORS = {
    "IndustryPrimaryExtractive": "rgba(139,90,43,0.8)",
    "IndustryPrimaryOrganic":    "rgba(34,139,34,0.8)",
    "IndustryPrimaryRanch":      "rgba(210,105,30,0.8)",
    "IndustryPrimaryPort":       "rgba(30,144,255,0.8)",
    "IndustryPrimaryNoSupplies": "rgba(128,128,128,0.8)",
    "IndustrySecondary":         "rgba(178,102,178,0.8)",
    "IndustryTertiary":          "rgba(220,20,60,0.8)",
}


# =====================================================================
# VIEW 1: SANKEY DIAGRAM
# =====================================================================

def _compute_best_producers() -> dict[str, dict[str, list[tuple[str, float]]]]:
    """Find the best producer(s) for each cargo, separately per tier.

    Returns ``{cargo_label: {"primary": [(ind, val), ...], "secondary": [(ind, val), ...]}}``
    where each list contains all industries tied for the highest value in that tier.

    Primary metric: ``weighted_average_base`` from production range.
    Secondary metric: normalized output per 8 units of input (all inputs present).
    """
    # Collect all (industry, value) pairs per cargo per tier
    primary_vals: dict[str, list[tuple[str, float]]] = {}
    secondary_vals: dict[str, list[tuple[str, float]]] = {}

    for ind_name, ind_data in PRIMARY.items():
        for cargo_label, cargo_info in ind_data["produces"].items():
            val = cargo_info["production_range"]["weighted_average_base"]
            primary_vals.setdefault(cargo_label, []).append((ind_name, val))

    for ind_name, ind_data in SECONDARY.items():
        scenarios = ind_data["production_per_8_units_input"]
        input_labels = [inp["label"] for inp in ind_data["input_cargos"]]
        n_inputs = len(input_labels)
        all_key = "+".join(input_labels)
        all_scenario = scenarios.get(all_key, {})

        for out in ind_data["output_cargos"]:
            cargo_label = out["label"]
            raw_total = sum(
                all_scenario.get(f"{cargo_label}_per_8_{il}", 0)
                for il in input_labels
            )
            normalized = round(raw_total / n_inputs, 1) if raw_total > 0 else 0
            if normalized > 0:
                secondary_vals.setdefault(cargo_label, []).append((ind_name, normalized))

    # For each cargo+tier, keep only the tied-for-best entries
    best: dict[str, dict[str, list[tuple[str, float]]]] = {}
    all_cargos = set(primary_vals) | set(secondary_vals)
    for cl in all_cargos:
        entry: dict[str, list[tuple[str, float]]] = {}
        if cl in primary_vals:
            max_val = max(v for _, v in primary_vals[cl])
            entry["primary"] = [(ind, v) for ind, v in primary_vals[cl] if v == max_val]
        if cl in secondary_vals:
            max_val = max(v for _, v in secondary_vals[cl])
            entry["secondary"] = [(ind, v) for ind, v in secondary_vals[cl] if v == max_val]
        best[cl] = entry

    return best


def build_sankey_figure():
    """Build a Sankey figure with best-producer stars and cargo metadata.

    Returns ``(fig, sankey_meta)`` where *sankey_meta* is a dict with:
      - ``cargo_link_map``: ``{cargo_label: [link_indices]}``
      - ``cargo_names``: ``{cargo_label: display_name}``
      - ``cargo_colors``: ``{cargo_label: css_color}``
      - ``industry_link_map``: ``{industry_name: [link_indices]}``
      - ``industry_tiers``: ``{industry_name: "primary"|"secondary"|"tertiary"}``
    """
    best_producers = _compute_best_producers()

    nodes_labels = []
    nodes_colors = []
    node_index = {}
    # Track which node keys are best producers for which cargos
    node_best_cargos: dict[str, list[str]] = {}  # node_key → [cargo_labels]

    def add_node(key, label, color):
        if key not in node_index:
            node_index[key] = len(nodes_labels)
            nodes_labels.append(label)
            nodes_colors.append(color)
        return node_index[key]

    links_source = []
    links_target = []
    links_value = []
    links_cargo_labels = []  # track cargo label per link for deferred color computation
    links_label = []
    links_industry = []  # track industry name per link for industry filtering

    def _parse_rgb(c):
        """Extract (r, g, b) from any color format."""
        if c.startswith("rgba("):
            parts = c[5:].rstrip(")").split(",")
            return int(parts[0]), int(parts[1]), int(parts[2])
        elif c.startswith("rgb("):
            parts = c[4:].rstrip(")").split(",")
            return int(parts[0]), int(parts[1]), int(parts[2])
        elif c.startswith("#"):
            h = c.lstrip("#")
            return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return 200, 200, 200

    def make_link_color(cargo_label, alpha=0.5):
        r, g, b = _parse_rgb(CARGO_COLORS.get(cargo_label, "rgb(200,200,200)"))
        return f"rgba({r},{g},{b},{alpha})"

    # -- Scaling constants for visual link thickness --
    PRIMARY_SCALE = 0.2
    SECONDARY_INPUT_SCALE = 6
    SECONDARY_OUTPUT_SCALE = 6
    TERTIARY_SINK_VALUE = 10

    # --- Primary industries → cargo nodes ---
    for ind_name, ind_data in sorted(PRIMARY.items()):
        ind_type = ind_data["type"]
        node_key = f"pri_{ind_name}"
        ind_idx = add_node(node_key, pretty(ind_name), TYPE_COLORS.get(ind_type, "grey"))

        for cargo_label, cargo_info in ind_data["produces"].items():
            cargo_idx = add_node(f"cargo_{cargo_label}", f"{cargo_info['cargo_name']} ({cargo_label})",
                                 CARGO_COLORS.get(cargo_label, "grey"))
            avg_prod = cargo_info["production_range"]["weighted_average_base"]
            links_source.append(ind_idx)
            links_target.append(cargo_idx)
            links_value.append(max(avg_prod * PRIMARY_SCALE, 1))
            links_cargo_labels.append(cargo_label)
            links_industry.append(ind_name)
            links_label.append(f"{pretty(ind_name)} → {cargo_info['cargo_name']}: ~{avg_prod} per cycle (weighted avg)")

            # Track best primary producer (all tied winners get stars)
            bp = best_producers.get(cargo_label, {})
            if any(ind == ind_name for ind, _ in bp.get("primary", [])):
                node_best_cargos.setdefault(node_key, []).append(cargo_label)

    # --- Cargo nodes → Secondary industries ---
    for ind_name, ind_data in sorted(SECONDARY.items()):
        node_key = f"sec_{ind_name}"
        ind_idx = add_node(node_key, pretty(ind_name), TYPE_COLORS["IndustrySecondary"])

        for inp in ind_data["input_cargos"]:
            cargo_label = inp["label"]
            cargo_idx = add_node(f"cargo_{cargo_label}", f"{inp['name']} ({cargo_label})",
                                 CARGO_COLORS.get(cargo_label, "grey"))
            links_source.append(cargo_idx)
            links_target.append(ind_idx)
            links_value.append(inp["base_ratio"] * SECONDARY_INPUT_SCALE)
            links_cargo_labels.append(cargo_label)
            links_industry.append(ind_name)
            links_label.append(f"{inp['name']} → {pretty(ind_name)} (input ratio {inp['base_ratio']}/8)")

        # Secondary outputs → cargo nodes (using all-inputs-combined, normalized)
        scenarios = ind_data["production_per_8_units_input"]
        all_input_labels = [inp["label"] for inp in ind_data["input_cargos"]]
        n_inputs = len(all_input_labels)
        all_key = "+".join(all_input_labels)
        all_scenario = scenarios.get(all_key, {})

        for out in ind_data["output_cargos"]:
            cargo_label = out["label"]
            cargo_idx = add_node(f"cargo_{cargo_label}", f"{out['name']} ({cargo_label})",
                                 CARGO_COLORS.get(cargo_label, "grey"))

            raw_total = 0
            for inp_label in all_input_labels:
                key = f"{cargo_label}_per_8_{inp_label}"
                raw_total += all_scenario.get(key, 0)
            normalized = round(raw_total / n_inputs, 1) if raw_total > 0 else 0

            links_source.append(ind_idx)
            links_target.append(cargo_idx)
            links_value.append(max(normalized * SECONDARY_OUTPUT_SCALE, 1))
            links_cargo_labels.append(cargo_label)
            links_industry.append(ind_name)
            links_label.append(
                f"{pretty(ind_name)} → {out['name']}: "
                f"{normalized} per 8 units of input (all inputs present, normalized)"
            )

            # Track best secondary producer (all tied winners get stars)
            bp = best_producers.get(cargo_label, {})
            if any(ind == ind_name for ind, _ in bp.get("secondary", [])):
                node_best_cargos.setdefault(node_key, []).append(cargo_label)

    # --- Cargo nodes → Tertiary industries ---
    for ind_name, ind_data in sorted(TERTIARY.items()):
        ind_idx = add_node(f"ter_{ind_name}", pretty(ind_name), TYPE_COLORS["IndustryTertiary"])

        for acc in ind_data["accepts"]:
            cargo_label = acc["label"]
            cargo_idx = add_node(f"cargo_{cargo_label}", f"{acc['name']} ({cargo_label})",
                                 CARGO_COLORS.get(cargo_label, "grey"))
            links_source.append(cargo_idx)
            links_target.append(ind_idx)
            links_value.append(TERTIARY_SINK_VALUE)
            links_cargo_labels.append(cargo_label)
            links_industry.append(ind_name)
            links_label.append(f"{acc['name']} → {pretty(ind_name)} (consumer sink)")

    # --- Build rich hover customdata for every node ---
    # node_key → hover HTML (industry nodes get detailed info, cargo nodes
    # get classification + payment properties).
    nodes_customdata = list(nodes_labels)  # default: display label

    # Build hover for PRIMARY industry nodes
    for ind_name, ind_data in PRIMARY.items():
        node_key = f"pri_{ind_name}"
        if node_key not in node_index:
            continue
        idx = node_index[node_key]
        ind_type_label = ind_data["type"].replace("IndustryPrimary", "Primary · ")
        lines = [f"<b>{pretty(ind_name)}</b>", f"Type: {ind_type_label}"]
        # Supply requirements
        sr = ind_data.get("supply_requirements")
        if sr:
            lines.append(f"Supply boost: L1 ≥{sr['level1_threshold_3months']} → {sr['level1_production_percent']}%, "
                         f"L2 ≥{sr['level2_threshold_3months']} → {sr['level2_production_percent']}%")
        else:
            lines.append("Supply boost: none (no supplies accepted)")
        # Production per cargo
        lines.append("─── Production ───")
        for cargo_label, cargo_info in ind_data["produces"].items():
            pr = cargo_info["production_range"]
            lines.append(
                f"  {cargo_info['cargo_name']} ({cargo_label}): "
                f"{pr['min_base']}–{pr['max_base']} base "
                f"(avg {pr['weighted_average_base']}), "
                f"L2: {pr['with_level2_supplies_min']}–{pr['with_level2_supplies_max']}"
            )
        # Best producer stars
        bp_lines = []
        for cl in node_best_cargos.get(node_key, []):
            cargo_name = CARGO_DEFS[cl]["name"] if cl in CARGO_DEFS else cl
            bp_lines.append(f"★ Best primary producer of {cargo_name} ({cl})")
        if bp_lines:
            lines.append("─── Best Producer ───")
            lines.extend(bp_lines)
        nodes_customdata[idx] = "<br>".join(lines)

    # Build hover for SECONDARY industry nodes
    for ind_name, ind_data in SECONDARY.items():
        node_key = f"sec_{ind_name}"
        if node_key not in node_index:
            continue
        idx = node_index[node_key]
        has_combo = ind_data["combined_cargos_boost_prod"]
        input_labels = [inp["label"] for inp in ind_data["input_cargos"]]
        n_inputs = len(input_labels)
        lines = [f"<b>{pretty(ind_name)}</b>", f"Type: Secondary"]
        lines.append(f"Combo boost: {'YES – delivering multiple cargos boosts output' if has_combo else 'No – each cargo processed independently'}")
        # Input ratios
        lines.append("─── Inputs ───")
        for inp in ind_data["input_cargos"]:
            lines.append(f"  {inp['name']} ({inp['label']}): ratio {inp['base_ratio']}/8")
        # Output production rules
        scenarios = ind_data["production_per_8_units_input"]
        all_key = "+".join(input_labels)
        all_scenario = scenarios.get(all_key, {})
        lines.append("─── Output per 8 units input ───")
        for out in ind_data["output_cargos"]:
            ol = out["label"]
            # Solo values
            solo_parts = []
            for il in input_labels:
                solo_scenario = scenarios.get(il, {})
                val = solo_scenario.get(f"{ol}_per_8_{il}", 0)
                solo_parts.append(f"{il}→{val}")
            # Combined values
            combo_parts = []
            for il in input_labels:
                val = all_scenario.get(f"{ol}_per_8_{il}", 0)
                combo_parts.append(f"{il}→{val}")
            # Normalized summary — keep floats for boost calc, round only on display
            raw_solo = sum(scenarios.get(il, {}).get(f"{ol}_per_8_{il}", 0) for il in input_labels)
            raw_combo = sum(all_scenario.get(f"{ol}_per_8_{il}", 0) for il in input_labels)
            solo_norm = raw_solo / n_inputs if raw_solo > 0 else 0.0
            combo_norm = raw_combo / n_inputs if raw_combo > 0 else 0.0
            lines.append(f"  {out['name']} ({ol}) [ratio {out['output_ratio']}/8]:")
            lines.append(f"    Solo: {', '.join(solo_parts)} (norm {round(solo_norm, 1)})")
            if n_inputs > 1:
                boost_str = ""
                if has_combo and solo_norm > 0:
                    boost_pct = round((combo_norm / solo_norm - 1) * 100)
                    boost_str = f" (+{boost_pct}%)" if boost_pct > 0 else ""
                lines.append(f"    Combined: {', '.join(combo_parts)} (norm {round(combo_norm, 1)}){boost_str}")
        # Best producer stars
        bp_lines = []
        for cl in node_best_cargos.get(node_key, []):
            cargo_name = CARGO_DEFS[cl]["name"] if cl in CARGO_DEFS else cl
            bp_lines.append(f"★ Best secondary producer of {cargo_name} ({cl})")
        if bp_lines:
            lines.append("─── Best Producer ───")
            lines.extend(bp_lines)
        nodes_customdata[idx] = "<br>".join(lines)

    # Build hover for TERTIARY industry nodes
    for ind_name, ind_data in TERTIARY.items():
        node_key = f"ter_{ind_name}"
        if node_key not in node_index:
            continue
        idx = node_index[node_key]
        lines = [f"<b>{pretty(ind_name)}</b>", "Type: Tertiary (Consumer)"]
        lines.append("─── Accepts ───")
        for acc in ind_data["accepts"]:
            lines.append(f"  {acc['name']} ({acc['label']})")
        nodes_customdata[idx] = "<br>".join(lines)

    # Build hover for CARGO nodes (classification + payment properties from JSON)
    for node_key, idx in node_index.items():
        if not node_key.startswith("cargo_"):
            continue
        cl = node_key[len("cargo_"):]
        cdef = CARGO_DEFS.get(cl, {})
        cargo_name = cdef.get("name", cl)
        lines = [f"<b>{cargo_name}</b>", f"Label: {cl}"]
        # Cargo classification
        cc = cdef.get("cargo_classes")
        is_freight = cdef.get("is_freight")
        weight = cdef.get("weight")
        tge = cdef.get("town_growth_effect")
        if cc is not None or is_freight is not None:
            lines.append("─── Classification ───")
            if cc is not None:
                classes_str = ", ".join(c.replace("CC_", "") for c in cc)
                lines.append(f"  Cargo classes: {classes_str}")
            if is_freight is not None:
                lines.append(f"  Freight: {'Yes' if is_freight else 'No (Pax/Mail)'}")
            if weight is not None:
                lines.append(f"  Weight per unit: {weight}t")
            if tge is not None:
                effect = tge.replace("TOWNGROWTH_", "")
                lines.append(f"  Town growth effect: {effect}")
        # Payment properties
        pf = cdef.get("price_factor")
        plb = cdef.get("penalty_lowerbound")
        spl = cdef.get("single_penalty_length")
        if pf is not None:
            lines.append("─── Payment Properties ───")
            lines.append(f"  Base price factor: {pf}")
            if plb is not None:
                lines.append(f"  Penalty lower bound: {plb} periods")
            if spl is not None:
                if spl >= 255:
                    lines.append(f"  Single penalty length: {spl} (no decay)")
                else:
                    lines.append(f"  Single penalty length: {spl} periods")
        nodes_customdata[idx] = "<br>".join(lines)

    # --- Append ★ markers to best-producer node labels ---
    for node_key, cargo_list in node_best_cargos.items():
        idx = node_index[node_key]
        clean_name = nodes_labels[idx]
        stars = " ".join("★" for _ in cargo_list)
        nodes_labels[idx] = f"{clean_name} {stars}"

    # --- Compute link colors with fixed opacity ---
    links_color = [make_link_color(cl, 0.5) for cl in links_cargo_labels]

    # --- Build cargo → link-indices map for JS filtering ---
    cargo_link_map: dict[str, list[int]] = {}
    for i, cl in enumerate(links_cargo_labels):
        cargo_link_map.setdefault(cl, []).append(i)

    cargo_names: dict[str, str] = {}
    for cl in sorted(cargo_link_map.keys()):
        if cl in CARGO_DEFS:
            cargo_names[cl] = CARGO_DEFS[cl]["name"]
        else:
            cargo_names[cl] = cl

    # --- Build industry → link-indices map for JS filtering ---
    industry_link_map: dict[str, list[int]] = {}
    for i, ind in enumerate(links_industry):
        industry_link_map.setdefault(ind, []).append(i)

    # Determine industry tier for grouping
    industry_tiers: dict[str, str] = {}
    for ind in industry_link_map:
        if ind in PRIMARY:
            industry_tiers[ind] = "primary"
        elif ind in SECONDARY:
            industry_tiers[ind] = "secondary"
        else:
            industry_tiers[ind] = "tertiary"

    fig = go.Figure(go.Sankey(
        arrangement="snap",
        node=dict(
            pad=12,
            thickness=18,
            line=dict(color="rgba(0,0,0,0.4)", width=0.5),
            label=nodes_labels,
            color=nodes_colors,
            customdata=nodes_customdata,
            hovertemplate="%{customdata}<extra></extra>",
        ),
        link=dict(
            source=links_source,
            target=links_target,
            value=links_value,
            color=links_color,
            label=links_label,
            hovertemplate="%{label}<extra></extra>",
        ),
    ))

    fig.update_layout(
        title=dict(
            text="NAIS Cargo Flow Network<br>"
                 "<sub>Primary (left) → Cargo → Secondary/Tertiary (right) · "
                 "★ = best producer per tier · Link thickness ∝ output · Drag nodes to rearrange</sub>",
            font=dict(size=16),
        ),
        template="plotly_white",
        height=1100,
        margin=dict(t=80, b=20, l=20, r=20),
        paper_bgcolor="white",
    )

    sankey_meta = {
        "cargo_link_map": cargo_link_map,
        "cargo_names": cargo_names,
        "cargo_colors": {cl: CARGO_COLORS.get(cl, "grey") for cl in cargo_link_map},
        "industry_link_map": industry_link_map,
        "industry_tiers": industry_tiers,
    }
    return fig, sankey_meta


# =====================================================================
# VIEW 2: PRIMARY PRODUCTION RANGES
# =====================================================================

def build_primary_figure():
    """Build box-plot chart for primary industry production ranges."""
    rows = []
    for ind_name, ind_data in sorted(PRIMARY.items()):
        for cargo_label, cargo_info in ind_data["produces"].items():
            rows.append({
                "industry": pretty(ind_name),
                "cargo": cargo_info["cargo_name"],
                "cargo_label": cargo_label,
                "type": ind_data["type"],
                "min_base": cargo_info["production_range"]["min_base"],
                "avg_base": cargo_info["production_range"]["weighted_average_base"],
                "max_base": cargo_info["production_range"]["max_base"],
                "min_l2": cargo_info["production_range"]["with_level2_supplies_min"],
                "avg_l2": round(cargo_info["production_range"]["weighted_average_base"] * 3),
                "max_l2": cargo_info["production_range"]["with_level2_supplies_max"],
            })

    # Sort by cargo label first (group by cargo), then by avg_base descending within each cargo
    rows.sort(key=lambda r: (r["cargo_label"], -r["avg_base"]))

    x_labels = [f"{r['industry']}<br>({r['cargo_label']})" for r in rows]

    fig = go.Figure()

    # Base production box plot (blue) — pre-computed statistics
    # Use invisible scatter traces for custom hover since Box hovertemplate
    # doesn't fully override the default labels for pre-computed stats
    fig.add_trace(go.Box(
        name="Base",
        x=x_labels,
        lowerfence=[r["min_base"] for r in rows],
        q1=[r["min_base"] for r in rows],
        median=[r["avg_base"] for r in rows],
        q3=[r["max_base"] for r in rows],
        upperfence=[r["max_base"] for r in rows],
        marker_color="rgba(65,105,225,0.85)",
        fillcolor="rgba(100,149,237,0.4)",
        line=dict(color="rgba(25,25,112,0.8)"),
        hoverinfo="skip",
    ))
    # Invisible scatter points at min, avg, max for Base hover across the full box
    for y_key in ("min_base", "avg_base", "max_base"):
        fig.add_trace(go.Scatter(
            name="Base",
            x=x_labels,
            y=[r[y_key] for r in rows],
            mode="markers",
            marker=dict(size=12, opacity=0, color="rgba(65,105,225,0.85)"),
            customdata=[[r["min_base"], r["avg_base"], r["max_base"]] for r in rows],
            hovertemplate=("<b>Base</b><br>Min: %{customdata[0]}<br>"
                           "Weighted Avg: %{customdata[1]}<br>"
                           "Max: %{customdata[2]}<extra>%{x}</extra>"),
            hoverlabel=dict(bgcolor="rgba(100,149,237,0.9)", font_color="white"),
            showlegend=False,
        ))

    # L2 Supply production box plot (orange) — pre-computed statistics
    fig.add_trace(go.Box(
        name="L2 Supply (3×)",
        x=x_labels,
        lowerfence=[r["min_l2"] for r in rows],
        q1=[r["min_l2"] for r in rows],
        median=[r["avg_l2"] for r in rows],
        q3=[r["max_l2"] for r in rows],
        upperfence=[r["max_l2"] for r in rows],
        marker_color="rgba(255,140,0,0.85)",
        fillcolor="rgba(255,165,0,0.35)",
        line=dict(color="rgba(255,69,0,0.8)"),
        hoverinfo="skip",
    ))
    # Invisible scatter points at min, avg, max for L2 hover across the full box
    for y_key in ("min_l2", "avg_l2", "max_l2"):
        fig.add_trace(go.Scatter(
            name="L2 Supply (3×)",
            x=x_labels,
            y=[r[y_key] for r in rows],
            mode="markers",
            marker=dict(size=12, opacity=0, color="rgba(255,140,0,0.85)"),
            customdata=[[r["min_l2"], r["avg_l2"], r["max_l2"]] for r in rows],
            hovertemplate=("<b>L2 Supply (3×)</b><br>Min: %{customdata[0]}<br>"
                           "Weighted Avg: %{customdata[1]}<br>"
                           "Max: %{customdata[2]}<extra>%{x}</extra>"),
            hoverlabel=dict(bgcolor="rgba(255,165,0,0.9)", font_color="white"),
            showlegend=False,
        ))

    # Highlight best (tied) producers per cargo type
    from collections import defaultdict
    cargo_groups = defaultdict(list)
    for i, r in enumerate(rows):
        cargo_groups[r["cargo_label"]].append((i, r))

    best_annotations = []
    for cargo_label, group in cargo_groups.items():
        max_avg = max(r["avg_base"] for _, r in group)
        for i, r in group:
            if r["avg_base"] == max_avg:
                best_annotations.append(dict(
                    x=x_labels[i],
                    y=r["max_l2"],  # place above the tallest box
                    text=f"★ {r['avg_base']}/{r['avg_l2']}",
                    showarrow=False,
                    yshift=10,
                    font=dict(size=9, color="darkred"),
                ))

    # Build alternating background shading and cargo group labels
    # For categorical x-axis, positions are 0-indexed integers
    shapes = []
    cargo_label_annotations = []
    shade_colors = ["rgba(240,240,255,0.5)", "rgba(255,245,230,0.5)"]
    color_idx = 0
    # Find cargo group boundaries
    prev_cargo = None
    group_start = 0
    cargo_boundaries = []  # list of (start_idx, end_idx, cargo_label, cargo_name)
    for i, r in enumerate(rows):
        if r["cargo_label"] != prev_cargo:
            if prev_cargo is not None:
                cargo_boundaries.append((group_start, i - 1, prev_cargo, rows[group_start]["cargo"]))
            group_start = i
            prev_cargo = r["cargo_label"]
    if prev_cargo is not None:
        cargo_boundaries.append((group_start, len(rows) - 1, prev_cargo, rows[group_start]["cargo"]))

    data_max = max(r["max_l2"] for r in rows)
    max_y = data_max * 1.14  # label placement baseline
    label_gap = data_max * 0.05  # small gap between high and low labels
    y_range_top = data_max * 1.22  # extra headroom for labels

    for start, end, cargo_label, cargo_name in cargo_boundaries:
        shapes.append(dict(
            type="rect",
            xref="x", yref="paper",
            x0=start - 0.5, x1=end + 0.5,
            y0=0, y1=1,
            fillcolor=shade_colors[color_idx % 2],
            line=dict(width=0),
            layer="below",
        ))
        # Add cargo group label at the top, alternating vertical position
        # Even labels at max_y, odd labels just below (max_y - label_gap)
        mid_x = (start + end) / 2
        label_y = max_y if color_idx % 2 == 0 else max_y - label_gap
        cargo_label_annotations.append(dict(
            x=mid_x,
            y=label_y,
            text=f"<b>{cargo_name}</b><br>({cargo_label})",
            showarrow=False,
            font=dict(size=8, color="rgba(80,80,80,0.9)"),
            yanchor="bottom",
        ))
        color_idx += 1

    all_annotations = best_annotations + cargo_label_annotations

    fig.update_layout(
        title=dict(
            text="NAIS Primary Industry Production Ranges<br>"
                 "<sub>Grouped by cargo · Base production (blue) vs Level 2 Supply boost ×3 (orange) · ★ = best avg (base/L2)</sub>",
            font=dict(size=16),
        ),
        xaxis=dict(title="Industry (Cargo)", tickangle=-45, tickfont=dict(size=8)),
        yaxis=dict(title="Production per Cycle", gridcolor="rgba(200,200,200,0.5)",
                   range=[0, y_range_top]),
        boxmode="group",
        template="plotly_white",
        height=900,
        margin=dict(t=80, b=200, l=60, r=20),
        legend=dict(orientation="h", yanchor="top", y=-0.18, xanchor="center", x=0.5, font=dict(size=10)),
        shapes=shapes,
        annotations=all_annotations,
        paper_bgcolor="white",
        plot_bgcolor="white",
    )
    return fig


# =====================================================================
# VIEW 3: SECONDARY CONVERSION HEATMAP
# =====================================================================

def build_heatmap_figure():
    """Build a heatmap of secondary industry output per 8 units of input.

    The raw JSON stores output per 8 units of *each* input cargo.  To make
    values comparable across industries with different numbers of inputs we
    normalize: ``total_across_inputs / n_inputs`` so the cell value
    represents output per 8 units of (evenly mixed) input.
    """
    industries = sorted(SECONDARY.keys())
    all_output_labels = set()
    for ind_name in industries:
        for out in SECONDARY[ind_name]["output_cargos"]:
            all_output_labels.add(out["label"])
    all_output_labels = sorted(all_output_labels)

    z_matrix = []
    hover_text = []
    for ind_name in industries:
        ind_data = SECONDARY[ind_name]
        scenarios = ind_data["production_per_8_units_input"]
        all_input_labels = [inp["label"] for inp in ind_data["input_cargos"]]
        n_inputs = len(all_input_labels)
        all_key = "+".join(all_input_labels)

        row = []
        hover_row = []
        for out_label in all_output_labels:
            raw_total = 0
            details = []
            if all_key in scenarios:
                scenario = scenarios[all_key]
                for inp_label in all_input_labels:
                    key = f"{out_label}_per_8_{inp_label}"
                    val = scenario.get(key, 0)
                    raw_total += val
                    if val > 0:
                        details.append(f"  8 {inp_label} → {val} {out_label}")

            normalized = round(raw_total / n_inputs, 1) if raw_total > 0 else 0
            row.append(normalized)
            if details:
                hover_row.append(
                    f"<b>{pretty(ind_name)}</b><br>"
                    f"Output: {CARGO_DEFS[out_label]['name']} ({out_label})<br>"
                    f"{normalized} per 8 units of input "
                    f"({raw_total} total ÷ {n_inputs} inputs)<br>"
                    f"{'<br>'.join(details)}"
                )
            else:
                hover_row.append(f"<b>{pretty(ind_name)}</b><br>{out_label}: not produced")

        z_matrix.append(row)
        hover_text.append(hover_row)

    # Transpose z_matrix and hover_text so industries are on x-axis, cargos on y-axis
    z_transposed = list(map(list, zip(*z_matrix)))
    hover_transposed = list(map(list, zip(*hover_text)))

    fig = go.Figure(go.Heatmap(
        z=z_transposed,
        x=[pretty(n) for n in industries],
        y=[f"{CARGO_DEFS[l]['name']} ({l})" for l in all_output_labels],
        text=hover_transposed,
        hovertemplate="%{text}<extra></extra>",
        colorscale="YlOrRd",
        colorbar=dict(title="Output per<br>8 units input<br>(all present,<br>normalized)"),
        zmin=0,
    ))

    # Highlight the best producer for each cargo (each row in transposed matrix)
    x_labels = [pretty(n) for n in industries]
    y_labels = [f"{CARGO_DEFS[l]['name']} ({l})" for l in all_output_labels]
    best_annotations = []
    for cargo_idx, cargo_row in enumerate(z_transposed):
        if not any(v > 0 for v in cargo_row):
            continue
        max_val = max(cargo_row)
        if max_val <= 0:
            continue
        # Find ALL industries tied for best producer of this cargo
        for ind_idx, val in enumerate(cargo_row):
            if val == max_val:
                best_annotations.append(dict(
                    x=x_labels[ind_idx],
                    y=y_labels[cargo_idx],
                    text=f"★ {max_val}",
                    showarrow=False,
                    font=dict(size=10, color="white", family="Arial Black"),
                ))

    fig.update_layout(
        title=dict(
            text="NAIS Secondary Industry Output Heatmap<br>"
                 "<sub>Output per 8 units of input (all inputs present, normalized by # inputs) · ★ = best producer</sub>",
            font=dict(size=16),
        ),
        xaxis=dict(title="Industry", tickfont=dict(size=9), tickangle=-45, side="bottom"),
        yaxis=dict(title="Output Cargo", tickfont=dict(size=9)),
        template="plotly_white",
        height=900,
        margin=dict(t=80, b=200, l=180, r=80),
        annotations=best_annotations,
        paper_bgcolor="white",
        plot_bgcolor="white",
    )
    return fig


# =====================================================================
# VIEW 4: COMBO BOOST COMPARISON
# =====================================================================

def build_combo_figure():
    """Build grouped bars comparing solo vs all-combined output per 8 units of input.

    Values are normalized by dividing the raw summed output by the number of
    input cargos so that the bar height represents output per 8 units of
    (evenly mixed) input, consistent with the heatmap.
    """
    industries_sorted = sorted(SECONDARY.keys())

    x_labels = []
    solo_totals = []
    combined_totals = []
    boost_pcts = []
    combined_flags = []

    for ind_name in industries_sorted:
        ind_data = SECONDARY[ind_name]
        scenarios = ind_data["production_per_8_units_input"]
        input_labels = [inp["label"] for inp in ind_data["input_cargos"]]
        output_labels = [out["label"] for out in ind_data["output_cargos"]]
        n_inputs = len(input_labels)

        # Solo: sum all outputs across each input delivered alone,
        # then normalize by n_inputs (total output per 8 units of input).
        # Keep floats for boost calc; round only on output.
        raw_solo = 0
        for inp_label in input_labels:
            if inp_label in scenarios:
                for out_label in output_labels:
                    key = f"{out_label}_per_8_{inp_label}"
                    raw_solo += scenarios[inp_label].get(key, 0)
        solo_norm = raw_solo / n_inputs

        # Combined: all inputs present, total output normalized by n_inputs
        all_key = "+".join(input_labels)
        raw_combined = 0
        if all_key in scenarios:
            for inp_label in input_labels:
                for out_label in output_labels:
                    key = f"{out_label}_per_8_{inp_label}"
                    raw_combined += scenarios[all_key].get(key, 0)
        combined_norm = raw_combined / n_inputs

        boost = ((combined_norm / solo_norm - 1) * 100) if solo_norm > 0 else 0

        x_labels.append(pretty(ind_name))
        solo_totals.append(round(solo_norm, 1))
        combined_totals.append(round(combined_norm, 1))
        boost_pcts.append(boost)
        combined_flags.append(ind_data["combined_cargos_boost_prod"])

    # Sort by combined_total descending
    order = sorted(range(len(x_labels)), key=lambda i: -combined_totals[i])
    x_labels = [x_labels[i] for i in order]
    solo_totals = [solo_totals[i] for i in order]
    combined_totals = [combined_totals[i] for i in order]
    boost_pcts = [boost_pcts[i] for i in order]
    combined_flags = [combined_flags[i] for i in order]

    fig = go.Figure()

    fig.add_trace(go.Bar(
        name="Solo Delivery (single input at a time)",
        x=x_labels, y=solo_totals,
        marker_color="rgba(100,149,237,0.75)",
        hovertemplate="%{x}<br>Solo output per 8 input: %{y}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        name="All Inputs Combined (delivered together)",
        x=x_labels, y=combined_totals,
        marker_color="rgba(255,140,0,0.85)",
        hovertemplate="%{x}<br>Combined output per 8 input: %{y}<br>Boost: %{customdata:.0f}%<extra></extra>",
        customdata=boost_pcts,
    ))

    # Boost annotations
    annotations = []
    for i, (label, boost, has_combo) in enumerate(zip(x_labels, boost_pcts, combined_flags)):
        if has_combo and boost > 0:
            annotations.append(dict(
                x=label, y=combined_totals[i],
                text=f"+{boost:.0f}%",
                showarrow=False, yshift=12,
                font=dict(size=9, color="darkred"),
            ))

    fig.update_layout(
        title=dict(
            text="NAIS Secondary Industry Combinatory Boost<br>"
                 "<sub>Total output per 8 units of input (normalized by # inputs) · Solo (blue) vs all inputs present (orange)</sub>",
            font=dict(size=16),
        ),
        xaxis=dict(title="Industry", tickangle=-45, tickfont=dict(size=8)),
        yaxis=dict(title="Total output per 8 units input (normalized)", gridcolor="rgba(200,200,200,0.5)"),
        barmode="group",
        template="plotly_white",
        height=900,
        margin=dict(t=80, b=200, l=60, r=20),
        legend=dict(orientation="h", yanchor="top", y=-0.18, xanchor="center", x=0.5, font=dict(size=10)),
        annotations=annotations,
        paper_bgcolor="white",
        plot_bgcolor="white",
    )
    return fig


