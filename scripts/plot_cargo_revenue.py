#!/usr/bin/env python3
"""
Plotly figure builder for NAIS cargo revenue as a function of distance (tiles).

Interactive chart with a slider to adjust average transit speed
(10–300 km/h) and a toggle between "Revenue per trip" and "Revenue per day".

Invoked by plot_dashboard.py to assemble the unified dashboard.

OpenTTD cargo payment formula (from economy.cpp GetTransportedGoodsIncome):
  revenue = price_factor / 200 * amount * distance * time_factor / 255

  (price_factor is the NML property; /200 normalizes from "10 units across 20 tiles")

Time factor (four regimes, post PR #10596):
  1. t <= d1:           time_factor = 255                    (flat, max pay)
  2. d1 < t <= d1+d2:   time_factor = 255 - (t - d1)        (slope -1/period)
  3. d1+d2 < t <= tmax: time_factor = 255 - 2(t-d1) + d2    (slope -2/period, floor 31)
  4. t > tmax:          time_factor = 31 / (x/(2*16) + 1)   (asymptotic → 1)

  Where tmax is the transit time at which the old formula would hit 31.
  In regime 4, the time factor uses fixed-point arithmetic (×16) and
  revenue is divided by an extra factor of 16.

Transit time conversion (from OpenTTD wiki):
  A tile is 664.216 km-ish long.  km-ish/h = km/h / 1.00584.
  tiles/day = speed_kmh * 24 / (664.216 * 1.00584) = speed_kmh / 27.84
  100 km/h ≈ 3.59 tiles/day (wiki says ~3.6 ✓)

  Cargo aging period = 185 ticks = 2.5 days.
  transit_days    = distance_tiles / (speed_kmh / 27.84)
  transit_periods = transit_days / 2.5

Verified against tt-forums.net/viewtopic.php?t=84913 MILK example:
  360k L MILK, 131 tiles, 54 days → periods=22, tf=227, revenue≈£30,437 ✓
"""

import json
import os

import plotly.graph_objects as go

from plot_industry_cargo import CARGO_COLORS

# ── Load cargo data from JSON ────────────────────────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_JSON_PATH = os.path.join(_PROJECT_ROOT, "data", "nais_production_data.json")

with open(_JSON_PATH) as _f:
    _DATA = json.load(_f)

_CARGO_DEFS = _DATA["cargo_definitions"]

# Build CARGOS list sorted by price_factor descending
CARGOS: list[tuple[str, str, int, int, int]] = []
for _label, _cdef in sorted(
    _CARGO_DEFS.items(),
    key=lambda kv: kv[1].get("price_factor", 0),
    reverse=True,
):
    _pf = _cdef.get("price_factor")
    _plb = _cdef.get("penalty_lowerbound")
    _spl = _cdef.get("single_penalty_length")
    if _pf is None or _plb is None or _spl is None:
        continue  # skip cargos without payment data
    CARGOS.append((_cdef["name"], _label, _pf, _plb, _spl))

# ── Constants ───────────────────────────────────────────────────────────
MAX_DISTANCE_TILES = 4096
DISTANCE_STEP = 8         # tile resolution for plotting
AMOUNT = 100              # units of cargo delivered (for readable y-axis)

# Speed slider range
SPEED_MIN = 10
SPEED_MAX = 300
SPEED_STEP = 10
SPEED_DEFAULT = 80        # km/h

# Conversion constants derived from OpenTTD wiki:
#   1 tile = 664.216 km-ish = 664.216 * 1.00584 km ≈ 668.1 km
#   tiles/day = speed_kmh / 27.84
#   1 cargo aging period = 2.5 days (185 ticks / 74 ticks-per-day)
KMISH_PER_TILE = 664.216
KMISH_TO_KMH = 1.00584
KM_PER_TILE = KMISH_PER_TILE * KMISH_TO_KMH  # ≈ 668.1
HOURS_PER_DAY = 24
DAYS_PER_PERIOD = 2.5

# tiles/day at v km/h = v * 24 / KM_PER_TILE
# transit_days = distance * KM_PER_TILE / (v * 24) = distance * DAY_FACTOR / v
DAY_FACTOR = KM_PER_TILE / HOURS_PER_DAY  # ≈ 27.84

NUM_CARGOS = len(CARGOS)


def calc_transit_days(distance_tiles, speed_kmh):
    """Calculate transit time in game days."""
    if speed_kmh <= 0:
        return float('inf')
    return distance_tiles * DAY_FACTOR / speed_kmh


def calc_transit_periods(distance_tiles, speed_kmh):
    """Calculate transit time in cargo-aging periods (1 period = 2.5 days)."""
    return calc_transit_days(distance_tiles, speed_kmh) / DAYS_PER_PERIOD


# Time factor constants from OpenTTD economy.cpp (post PR #10596)
MIN_TIME_FACTOR = 31
MAX_TIME_FACTOR = 255
TIME_FACTOR_FRAC_BITS = 4
TIME_FACTOR_FRAC = 1 << TIME_FACTOR_FRAC_BITS  # = 16


def _compute_time_factor(transit_periods, penalty_lowerbound, single_penalty_length):
    """
    OpenTTD time-based payment factor (post PR #10596).
    Four regimes from economy.cpp GetTransportedGoodsIncome:

      1. t <= d1:           time_factor = 255                    (flat, max pay)
      2. d1 < t <= d1+d2:   time_factor = 255 - (t - d1)        (slope -1)
      3. d1+d2 < t <= tmax: time_factor = 255 - 2(t-d1) + d2    (slope -2, floor at 31)
      4. t > tmax:          time_factor = 31*FRAC / (x/(2*FRAC)+1)  (asymptotic → 1)

    Returns (effective_time_factor, is_asymptotic).
    In the asymptotic regime, the factor is in fixed-point (×FRAC) and revenue
    must be divided by an extra FRAC.
    """
    t = transit_periods
    d1 = penalty_lowerbound
    d2 = single_penalty_length

    days_over_days1 = max(t - d1, 0)
    days_over_days2 = max(days_over_days1 - d2, 0)

    # Calculate days_over_max: how far past the point where old formula hits 31
    days_over_max = MIN_TIME_FACTOR - MAX_TIME_FACTOR  # = -224
    if d2 > -(MIN_TIME_FACTOR - MAX_TIME_FACTOR):  # d2 > 224
        days_over_max += t - d1
    else:
        days_over_max += 2 * (t - d1) - d2

    if days_over_max > 0:
        # Asymptotic regime: MIN_TIME_FACTOR / (x/(2*FRAC) + 1)
        # Expressed in fixed-point with TIME_FACTOR_FRAC_BITS
        tf_fixed = max(
            2 * MIN_TIME_FACTOR * TIME_FACTOR_FRAC * TIME_FACTOR_FRAC / (days_over_max + 2 * TIME_FACTOR_FRAC),
            1
        )
        return (tf_fixed, True)
    else:
        # Original double-decay (regimes 1-3)
        tf = max(MAX_TIME_FACTOR - days_over_days1 - days_over_days2, MIN_TIME_FACTOR)
        return (tf, False)


def revenue_per_trip(distance_tiles, speed_kmh, price_factor, penalty_lowerbound, single_penalty_length, amount=AMOUNT):
    """
    Revenue for a single trip.
    Base formula: price_factor / 200 * amount * distance * time_factor / 255
    In asymptotic regime (PR #10596): extra /FRAC divisor for fixed-point time_factor.
    """
    if distance_tiles <= 0:
        return 0
    tp = calc_transit_periods(distance_tiles, speed_kmh)
    tf, is_asymptotic = _compute_time_factor(tp, penalty_lowerbound, single_penalty_length)
    rev = price_factor / 200.0 * amount * distance_tiles * tf / 255.0
    if is_asymptotic:
        rev /= TIME_FACTOR_FRAC
    return rev


def revenue_per_day(distance_tiles, speed_kmh, price_factor, penalty_lowerbound, single_penalty_length, amount=AMOUNT):
    """Revenue rate: revenue per game day (= revenue_per_trip / transit_days)."""
    if distance_tiles <= 0:
        return 0
    rev = revenue_per_trip(distance_tiles, speed_kmh, price_factor, penalty_lowerbound, single_penalty_length, amount)
    td = calc_transit_days(distance_tiles, speed_kmh)
    if td <= 0:
        return 0
    return rev / td


def build_figure():
    """Build the Plotly figure with speed slider and per-trip/per-day toggle."""
    distances = list(range(DISTANCE_STEP, MAX_DISTANCE_TILES + 1, DISTANCE_STEP))
    speeds = list(range(SPEED_MIN, SPEED_MAX + 1, SPEED_STEP))

    fig = go.Figure()

    # Layout: for each speed, we create 2 * NUM_CARGOS traces:
    #   first NUM_CARGOS = per-trip traces
    #   next  NUM_CARGOS = per-day traces
    speed_mode_indices = {}
    trace_idx = 0

    for speed in speeds:
        for mode in ("trip", "day"):
            indices = []
            for ci, (name, label, pf, plb, spl) in enumerate(CARGOS):
                if mode == "trip":
                    ys = [revenue_per_trip(d, speed, pf, plb, spl) for d in distances]
                    y_label = "Revenue"
                    y_unit = "£"
                else:
                    ys = [revenue_per_day(d, speed, pf, plb, spl) for d in distances]
                    y_label = "Revenue/day"
                    y_unit = "£/day"

                tp_values = [calc_transit_periods(d, speed) if d > 0 else 0 for d in distances]
                td_values = [calc_transit_days(d, speed) if d > 0 else 0 for d in distances]

                # Build structured hover matching Sankey format
                cdef = _CARGO_DEFS.get(label, {})
                cc = cdef.get("cargo_classes")
                is_freight = cdef.get("is_freight")
                weight = cdef.get("weight")
                tge = cdef.get("town_growth_effect")
                hover_lines = [f"<b>{name}</b>", f"Label: {label}"]
                # Classification section
                if cc is not None or is_freight is not None:
                    hover_lines.append("─── Classification ───")
                    if cc is not None:
                        classes_str = ", ".join(c.replace("CC_", "") for c in cc)
                        hover_lines.append(f"  Cargo classes: {classes_str}")
                    if is_freight is not None:
                        hover_lines.append(f"  Freight: {'Yes' if is_freight else 'No (Pax/Mail)'}")
                    if weight is not None:
                        hover_lines.append(f"  Weight per unit: {weight}t")
                    if tge is not None:
                        effect = tge.replace("TOWNGROWTH_", "")
                        hover_lines.append(f"  Town growth effect: {effect}")
                # Payment section
                hover_lines.append("─── Payment Properties ───")
                hover_lines.append(f"  Base price factor: {pf}")
                hover_lines.append(f"  Penalty lower bound: {plb} periods")
                if spl >= 255:
                    hover_lines.append(f"  Single penalty length: {spl} (no decay)")
                else:
                    hover_lines.append(f"  Single penalty length: {spl} periods")
                # Revenue section (dynamic per-point data)
                hover_lines.append("─── Revenue ───")
                hover_lines.append(f"  Distance: %{{x}} tiles")
                hover_lines.append(f"  {y_label}: {y_unit}%{{y:,.1f}}")
                hover_lines.append(f"  Speed: {speed} km/h")
                hover_lines.append(
                    f"  Transit: %{{customdata[0]:.1f}} days "
                    f"(%{{customdata[1]:.1f}} periods)"
                )
                hover_html = "<br>".join(hover_lines) + "<extra></extra>"

                visible = (speed == SPEED_DEFAULT and mode == "trip")
                fig.add_trace(go.Scatter(
                    x=distances,
                    y=ys,
                    mode='lines',
                    name=f"{name}",
                    line=dict(color=CARGO_COLORS.get(label, "grey"), width=2),
                    visible=visible,
                    legendgroup=name,
                    showlegend=(speed == SPEED_DEFAULT and mode == "trip"),
                    hovertemplate=hover_html,
                    customdata=list(zip(td_values, tp_values)),
                ))
                indices.append(trace_idx)
                trace_idx += 1
            speed_mode_indices[(speed, mode)] = indices

    total_traces = trace_idx

    def make_visibility(speed, mode):
        vis = [False] * total_traces
        for idx in speed_mode_indices[(speed, mode)]:
            vis[idx] = True
        return vis

    # Slider steps for each mode
    trip_steps = []
    for speed in speeds:
        vis = make_visibility(speed, "trip")
        trip_steps.append(dict(
            method="update",
            args=[{"visible": vis, "showlegend": vis}],
            label=f"{speed}",
        ))

    day_steps = []
    for speed in speeds:
        vis = make_visibility(speed, "day")
        day_steps.append(dict(
            method="update",
            args=[{"visible": vis, "showlegend": vis}],
            label=f"{speed}",
        ))

    default_speed_idx = speeds.index(SPEED_DEFAULT)

    sliders = [dict(
        active=default_speed_idx,
        currentvalue=dict(
            prefix="Average Speed: ",
            suffix=" km/h",
            font=dict(size=16),
        ),
        pad=dict(t=100),
        steps=trip_steps,
    )]

    # Toggle buttons — positioned below the plot, above the speed slider
    updatemenus = [dict(
        type="buttons",
        direction="left",
        x=0.5,
        y=-0.12,
        xanchor="center",
        yanchor="top",
        buttons=[
            dict(
                label="💰 Revenue per Trip",
                method="update",
                args=[
                    {"visible": make_visibility(SPEED_DEFAULT, "trip"),
                     "showlegend": make_visibility(SPEED_DEFAULT, "trip")},
                    {"yaxis.title.text": f"Revenue per Trip (£, per {AMOUNT} units)",
                     "sliders": [dict(
                         active=default_speed_idx,
                         currentvalue=dict(prefix="Average Speed: ", suffix=" km/h", font=dict(size=16)),
                         pad=dict(t=100),
                         steps=trip_steps,
                     )]},
                ],
            ),
            dict(
                label="⏱️ Revenue per Day",
                method="update",
                args=[
                    {"visible": make_visibility(SPEED_DEFAULT, "day"),
                     "showlegend": make_visibility(SPEED_DEFAULT, "day")},
                    {"yaxis.title.text": f"Revenue per Day (£/day, per {AMOUNT} units)",
                     "sliders": [dict(
                         active=default_speed_idx,
                         currentvalue=dict(prefix="Average Speed: ", suffix=" km/h", font=dict(size=16)),
                         pad=dict(t=100),
                         steps=day_steps,
                     )]},
                ],
            ),
        ],
        font=dict(size=13),
        bgcolor="rgba(240,240,240,0.9)",
        bordercolor="rgba(0,0,0,0.3)",
    )]

    fig.update_layout(
        title=dict(
            text=f"NAIS Cargo Revenue per {AMOUNT} Units vs Distance<br>"
                 f"<sub>OpenTTD payment formula (four-regime time factor) · "
                 f"100 km/h ≈ 3.6 tiles/day · "
                 f"1 aging period = 2.5 days · no inflation</sub>",
            font=dict(size=16),
        ),
        xaxis=dict(
            title="Distance (tiles)",
            range=[0, MAX_DISTANCE_TILES],
            gridcolor='rgba(200,200,200,0.5)',
            zeroline=True,
            zerolinecolor='rgba(0,0,0,0.3)',
        ),
        yaxis=dict(
            title=f"Revenue per Trip (£, per {AMOUNT} units)",
            gridcolor='rgba(200,200,200,0.5)',
            zeroline=True,
            zerolinecolor='rgba(0,0,0,0.3)',
        ),
        sliders=sliders,
        updatemenus=updatemenus,
        legend=dict(
            title="Cargo (click to toggle)",
            font=dict(size=10),
            itemclick="toggle",
            itemdoubleclick="toggleothers",
        ),
        template="plotly_white",
        height=900,
        margin=dict(b=180, t=80),
        paper_bgcolor="white",
    )

    return fig


