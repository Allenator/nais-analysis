#!/usr/bin/env python3
"""
Unified NAIS Dashboard — combines all visualizations into a single
tabbed HTML page with a consistent theme.

Tabs:
  1. 💰 Cargo Revenue — revenue vs distance with speed slider
  2. 🔀 Cargo Flow (Sankey) — full cargo flow network
  3. 📊 Primary Production — box plots of production ranges
  4. 🔥 Secondary Heatmap — conversion output heatmap
  5. ⚡ Combo Boost — solo vs combined delivery comparison

Reads:  data/nais_production_data.json (via plot_industry_cargo)
Writes: figures/nais_dashboard.html
"""

import json
import os
import subprocess
import sys

# Ensure scripts/ is on the import path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import plotly.graph_objects as go

# Import figure builders from sibling scripts
from plot_cargo_revenue import build_figure as build_revenue_figure
from plot_industry_cargo import (
    build_sankey_figure,
    build_primary_figure,
    build_heatmap_figure,
    build_combo_figure,
)

# ── Paths ────────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
FIGURES_DIR = os.path.join(PROJECT_ROOT, "dashboard")
JSON_PATH = os.path.join(DATA_DIR, "nais_production_data.json")
os.makedirs(FIGURES_DIR, exist_ok=True)

# ── Unified theme applied to every figure ────────────────────────────────
THEME = dict(
    paper_bgcolor="#fafbfc",
    plot_bgcolor="#ffffff",
    font=dict(family='-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif'),
)

ACCENT = "#2563eb"       # primary blue
ACCENT_DARK = "#1d4ed8"  # darker blue for active state
BG_LIGHT = "#fafbfc"     # page background


def apply_theme(fig: go.Figure) -> go.Figure:
    """Apply the unified NAIS theme to a Plotly figure."""
    fig.update_layout(**THEME)
    return fig


def get_nais_version() -> str:
    """Read the NAIS version from the production-data JSON metadata."""
    try:
        with open(JSON_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("_metadata", {}).get("nais_version", "unknown")
    except (FileNotFoundError, json.JSONDecodeError):
        return "unknown"


def _git_info(repo_path: str) -> str:
    """Return a short commit description + dirty flag for a git repo.

    Format: ``abc1234 (dirty)`` or ``abc1234`` (clean).
    Returns ``"unknown"`` on any failure.
    """
    try:
        short_hash = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_path,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        # Check for uncommitted changes (staged or unstaged)
        dirty = subprocess.call(
            ["git", "diff", "--quiet", "HEAD"],
            cwd=repo_path,
            stderr=subprocess.DEVNULL,
        ) != 0
        return f"{short_hash} (modified)" if dirty else short_hash
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return "unknown"


def get_commit_info() -> dict[str, str]:
    """Gather git commit info for the dashboard repo and the NAIS submodule."""
    dashboard_info = _git_info(PROJECT_ROOT)
    nais_submodule = os.path.join(PROJECT_ROOT, "nais")
    nais_info = _git_info(nais_submodule)
    return {"dashboard": dashboard_info, "nais": nais_info}


def build_dashboard_html(
    figures: dict[str, tuple[str, go.Figure]],
    nais_version: str = "unknown",
    sankey_meta: dict | None = None,
    commit_info: dict[str, str] | None = None,
) -> str:
    """
    Build a single HTML page with tab buttons that switch between
    multiple Plotly figures.

    Parameters
    ----------
    figures : dict
        Mapping of tab_id → (tab_label, go.Figure).
    nais_version : str
        NAIS version string to display in the header.
    sankey_meta : dict, optional
        Metadata from build_sankey_figure() for cargo filtering controls.
    commit_info : dict, optional
        Git commit info with keys ``"dashboard"`` and ``"nais"``.
    """
    # Extract commit info for footer
    commit_nais = (commit_info or {}).get("nais", "unknown")
    commit_dashboard = (commit_info or {}).get("dashboard", "unknown")

    # Generate individual div HTML for each figure
    divs = {}
    for tab_id, (_, fig) in figures.items():
        div_html = fig.to_html(
            include_plotlyjs=False,
            full_html=False,
            div_id=f"plot-{tab_id}",
        )
        divs[tab_id] = div_html

    tab_buttons_html = ""
    for i, (tab_id, (label, _)) in enumerate(figures.items()):
        active_class = ' class="active"' if i == 0 else ""
        tab_buttons_html += (
            f'<button{active_class} onclick="switchTab(\'{tab_id}\')" '
            f'id="btn-{tab_id}">{label}</button>\n'
        )

    # Build Sankey filter controls HTML if metadata is available
    sankey_filter_html = ""
    sankey_js_data = ""
    if sankey_meta:
        import json as _json
        cargo_link_map = sankey_meta["cargo_link_map"]
        cargo_names = sankey_meta["cargo_names"]
        cargo_colors = sankey_meta["cargo_colors"]
        industry_link_map = sankey_meta["industry_link_map"]
        industry_tiers = sankey_meta["industry_tiers"]

        # Build cargo checkbox list sorted by cargo name
        sorted_cargos = sorted(cargo_link_map.keys(), key=lambda c: cargo_names.get(c, c))
        cargo_checkboxes = ""
        for cl in sorted_cargos:
            name = cargo_names.get(cl, cl)
            color = cargo_colors.get(cl, "#888")
            cargo_checkboxes += (
                f'<label class="filter-cb cargo-cb" style="border-left:3px solid {color}">'
                f'<input type="checkbox" checked data-cargo="{cl}" '
                f'onchange="filterSankey()"> {name} ({cl})</label>\n'
            )

        # Build industry checkbox list grouped by tier
        tier_colors = {"primary": "#8b5a2b", "secondary": "#b266b2", "tertiary": "#dc143c"}
        industry_checkboxes = ""
        for tier in ["primary", "secondary", "tertiary"]:
            tier_industries = sorted(
                [ind for ind, t in industry_tiers.items() if t == tier],
                key=lambda x: x.replace("_", " ").title()
            )
            if not tier_industries:
                continue
            tc = tier_colors.get(tier, "#888")
            for ind in tier_industries:
                display = ind.replace("_", " ").title()
                industry_checkboxes += (
                    f'<label class="filter-cb industry-cb" style="border-left:3px solid {tc}" data-tier="{tier}">'
                    f'<input type="checkbox" checked data-industry="{ind}" '
                    f'onchange="filterSankey()"> {display}</label>\n'
                )

        # Filter button + dropdown lives in the tab bar, not in the tab content
        sankey_filter_html = f"""
            <div class="sankey-filter-wrapper" id="sankey-filter-wrapper" style="display:none">
                <button class="sankey-filter-toggle" onclick="toggleFilterDropdown(event)">🔍 Filter ▾</button>
                <div class="sankey-filter-dropdown" id="sankey-filter-dropdown">
                    <input type="text" id="sankey-search" placeholder="🔍 Search cargos or industries..."
                           oninput="filterSankeySearch(this.value)">
                    <div class="filter-section">
                        <div class="filter-header">
                            <span class="filter-title">Cargos</span>
                            <button class="filter-btn" onclick="toggleAllGroup('cargo', true)">All</button>
                            <button class="filter-btn" onclick="toggleAllGroup('cargo', false)">None</button>
                        </div>
                        <div class="filter-checkboxes" id="cargo-checkboxes">
                            {cargo_checkboxes}
                        </div>
                    </div>
                    <div class="filter-section">
                        <div class="filter-header">
                            <span class="filter-title">Industries</span>
                            <button class="filter-btn" onclick="toggleAllGroup('industry', true)">All</button>
                            <button class="filter-btn" onclick="toggleAllGroup('industry', false)">None</button>
                        </div>
                        <div class="filter-checkboxes" id="industry-checkboxes">
                            {industry_checkboxes}
                        </div>
                    </div>
                </div>
            </div>
        """

        sankey_js_data = f"""
        var sankeyCargoLinkMap = {_json.dumps(cargo_link_map)};
        var sankeyIndustryLinkMap = {_json.dumps(industry_link_map)};
        """

    divs_html = ""
    for i, (tab_id, div_html) in enumerate(divs.items()):
        display = "block" if i == 0 else "none"
        divs_html += f'<div id="tab-{tab_id}" style="display:{display}">{div_html}</div>\n'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>NAIS Dashboard — North American Industry Set</title>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🚂</text></svg>">
    <script src="https://cdn.plot.ly/plotly-3.0.1.min.js"></script>
    <style>
        *, *::before, *::after {{
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                         Helvetica, Arial, sans-serif;
            margin: 0;
            padding: 0;
            background: {BG_LIGHT};
            color: #1a1a2e;
            display: flex;
            flex-direction: column;
            min-height: 100vh;
        }}

        /* ── Header ─────────────────────────────────────────── */
        .header {{
            background: linear-gradient(135deg, {ACCENT} 0%, {ACCENT_DARK} 100%);
            color: white;
            padding: 18px 24px 14px;
            text-align: center;
            box-shadow: 0 2px 8px rgba(0,0,0,0.12);
        }}
        .header h1 {{
            margin: 0 0 4px;
            font-size: 22px;
            font-weight: 700;
            letter-spacing: 0.5px;
        }}
        .header p {{
            margin: 0;
            font-size: 13px;
            opacity: 0.85;
        }}

        /* ── Tab bar ────────────────────────────────────────── */
        .tab-bar {{
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 6px;
            padding: 12px 20px 10px;
            background: white;
            border-bottom: 2px solid #e5e7eb;
            position: sticky;
            top: 0;
            z-index: 100;
            flex-wrap: wrap;
            box-shadow: 0 1px 3px rgba(0,0,0,0.04);
        }}
        .tab-bar button {{
            padding: 9px 18px;
            border: 1px solid #d1d5db;
            border-radius: 6px;
            background: #f9fafb;
            cursor: pointer;
            font-size: 13px;
            font-weight: 500;
            color: #374151;
            transition: all 0.15s ease;
        }}
        .tab-bar button:hover {{
            background: #f3f4f6;
            border-color: #9ca3af;
            color: #111827;
        }}
        .tab-bar button.active {{
            background: {ACCENT};
            color: white;
            border-color: {ACCENT_DARK};
            box-shadow: 0 1px 4px rgba(37,99,235,0.3);
        }}

        /* ── Plot container ─────────────────────────────────── */
        .plot-container {{
            width: 100%;
            margin: 0 auto;
            padding: 12px 20px 24px;
            flex: 1;
        }}
        .plot-container .plotly-graph-div {{
            width: 100% !important;
        }}

        /* ── Sankey filter (in tab bar, right-aligned) ───── */
        .sankey-filter-wrapper {{
            position: absolute;
            right: 20px;
            top: 50%;
            transform: translateY(-50%);
        }}
        .sankey-filter-toggle {{
            padding: 9px 18px;
            border: 1px solid #d1d5db;
            border-radius: 6px;
            background: #f9fafb;
            cursor: pointer;
            font-size: 13px;
            font-weight: 500;
            color: #374151;
            transition: all 0.15s ease;
        }}
        .sankey-filter-toggle:hover {{
            background: #f3f4f6;
            border-color: #9ca3af;
            color: #111827;
        }}
        .sankey-filter-dropdown {{
            display: none;
            position: absolute;
            top: 100%;
            right: 0;
            z-index: 200;
            min-width: 420px;
            max-width: 600px;
            padding: 12px 16px;
            background: white;
            border: 1px solid #d1d5db;
            border-radius: 8px;
            box-shadow: 0 8px 24px rgba(0,0,0,0.15);
            margin-top: 4px;
        }}
        .sankey-filter-dropdown.open {{
            display: block;
        }}
        .sankey-filter-dropdown input[type="text"] {{
            width: 100%;
            padding: 8px 12px;
            border: 1px solid #d1d5db;
            border-radius: 6px;
            font-size: 13px;
            margin-bottom: 8px;
            outline: none;
        }}
        .sankey-filter-dropdown input[type="text"]:focus {{
            border-color: {ACCENT};
            box-shadow: 0 0 0 2px rgba(37,99,235,0.15);
        }}
        .filter-section {{
            margin-bottom: 8px;
        }}
        .filter-header {{
            display: flex;
            align-items: center;
            gap: 6px;
            margin-bottom: 4px;
        }}
        .filter-title {{
            font-size: 12px;
            font-weight: 600;
            color: #374151;
        }}
        .filter-btn {{
            padding: 4px 10px;
            border: 1px solid #d1d5db;
            border-radius: 4px;
            background: white;
            cursor: pointer;
            font-size: 11px;
            color: #374151;
        }}
        .filter-btn:hover {{
            background: #f3f4f6;
        }}
        .filter-checkboxes {{
            display: flex;
            flex-wrap: wrap;
            gap: 4px;
            max-height: 150px;
            overflow-y: auto;
        }}
        .filter-cb {{
            display: inline-flex;
            align-items: center;
            gap: 3px;
            padding: 2px 8px 2px 6px;
            font-size: 11px;
            background: white;
            border: 1px solid #e5e7eb;
            border-radius: 4px;
            cursor: pointer;
            white-space: nowrap;
        }}
        .filter-cb:hover {{
            background: #f3f4f6;
        }}
        .filter-cb.hidden {{
            display: none;
        }}

        /* ── Footer ─────────────────────────────────────────── */
        .footer {{
            text-align: center;
            padding: 16px 20px;
            font-size: 11px;
            color: #9ca3af;
            border-top: 1px solid #e5e7eb;
            background: white;
        }}
        .footer a {{
            color: {ACCENT};
            text-decoration: none;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🚂 NAIS — North American Industry Set <span style="font-size:14px;font-weight:400;opacity:0.8">v{nais_version}</span></h1>
        <p>Interactive dashboard · Cargo economics, production ranges, industry flows &amp; conversion analysis</p>
    </div>
    <div class="tab-bar">
        {tab_buttons_html}
        {sankey_filter_html}
    </div>
    <div class="plot-container">
        {divs_html}
    </div>
    <div class="footer">
        Data sourced from <a href="https://www.tt-forums.net/viewtopic.php?t=84039">NAIS</a> for
        <a href="https://www.openttd.org/">OpenTTD</a> ·
        Built with <a href="https://plotly.com/python/">Plotly</a> ·
        By <a href="https://github.com/Allenator">Allenator</a>
        <br>
        NAIS commit: <code>{commit_nais}</code> · Dashboard commit: <code>{commit_dashboard}</code>
    </div>
    <script>
        function switchTab(tabId) {{
            // Hide all tabs
            document.querySelectorAll('[id^="tab-"]').forEach(el => el.style.display = 'none');
            // Deactivate all buttons
            document.querySelectorAll('.tab-bar button').forEach(btn => btn.classList.remove('active'));
            // Show selected tab
            document.getElementById('tab-' + tabId).style.display = 'block';
            document.getElementById('btn-' + tabId).classList.add('active');
            // Show/hide Sankey filter button
            var fw = document.getElementById('sankey-filter-wrapper');
            if (fw) {{
                fw.style.display = (tabId === 'sankey') ? '' : 'none';
                // Close dropdown when switching away
                var dd = document.getElementById('sankey-filter-dropdown');
                if (dd) dd.classList.remove('open');
            }}
            // Trigger Plotly resize for proper rendering
            var plotDiv = document.querySelector('#tab-' + tabId + ' .plotly-graph-div');
            if (plotDiv) {{
                Plotly.Plots.resize(plotDiv);
            }}
        }}

        // ── Revenue plot: sync speed slider with trip/day toggle ──
        // After the Plotly updatemenus button resets the slider to default,
        // re-apply the previously selected speed by re-triggering the slider step.
        document.addEventListener('DOMContentLoaded', function() {{
            var revPlot = document.getElementById('plot-revenue');
            if (!revPlot) return;

            var lastSliderIdx = null;

            // Track slider changes to remember the current speed index
            revPlot.on('plotly_sliderchange', function(evt) {{
                if (evt && evt.slider && evt.slider.active !== undefined) {{
                    lastSliderIdx = evt.slider.active;
                }}
            }});

            // After a button click (trip/day toggle), restore the slider position
            revPlot.on('plotly_buttonclicked', function(evt) {{
                if (lastSliderIdx === null) return;
                // The button just reset the slider to default; restore previous speed
                setTimeout(function() {{
                    var layout = revPlot.layout;
                    if (layout.sliders && layout.sliders[0] && layout.sliders[0].steps) {{
                        var step = layout.sliders[0].steps[lastSliderIdx];
                        if (step) {{
                            // Apply the step's args to restore the correct speed traces
                            Plotly.update(revPlot, step.args[0], {{}});
                            // Update the slider's active index visually
                            Plotly.relayout(revPlot, {{'sliders[0].active': lastSliderIdx}});
                        }}
                    }}
                }}, 50);
            }});
        }});

        // ── Sankey: perturb a node on initial load to trigger rearrangement ──
        document.addEventListener('DOMContentLoaded', function() {{
            var sankeyPlot = document.getElementById('plot-sankey');
            if (!sankeyPlot || !sankeyPlot.data || !sankeyPlot.data[0]) return;
            // Wait for Plotly to finish rendering, then nudge node 0
            setTimeout(function() {{
                var nodeData = sankeyPlot.data[0].node;
                if (!nodeData) return;
                var n = nodeData.label ? nodeData.label.length : 0;
                if (n === 0) return;
                // Build x/y arrays with a tiny perturbation on the first node
                var xs = new Array(n);
                var ys = new Array(n);
                for (var i = 0; i < n; i++) {{
                    xs[i] = undefined;
                    ys[i] = undefined;
                }}
                // Perturb first node slightly to trigger Plotly's snap rearrangement
                ys[0] = 0.001;
                Plotly.restyle(sankeyPlot, {{
                    'node.x': [xs],
                    'node.y': [ys]
                }}, [0]);
            }}, 200);
        }});

        // ── Sankey cargo filtering ──────────────────────────────
        {sankey_js_data}
        var sankeyOriginalValues = null;
        var sankeyOriginalColors = null;

        function getSankeyPlot() {{
            return document.getElementById('plot-sankey');
        }}

        function initSankeyOriginals() {{
            if (sankeyOriginalValues) return;
            var plot = getSankeyPlot();
            if (!plot || !plot.data || !plot.data[0]) return;
            sankeyOriginalValues = plot.data[0].link.value.slice();
            sankeyOriginalColors = plot.data[0].link.color.slice();
        }}

        function filterSankey() {{
            initSankeyOriginals();
            var plot = getSankeyPlot();
            if (!plot || !sankeyOriginalValues) return;

            // Gather unchecked cargos
            var uncheckedCargos = new Set();
            document.querySelectorAll('#cargo-checkboxes input[type="checkbox"]').forEach(function(cb) {{
                if (!cb.checked) uncheckedCargos.add(cb.dataset.cargo);
            }});

            // Gather unchecked industries
            var uncheckedIndustries = new Set();
            document.querySelectorAll('#industry-checkboxes input[type="checkbox"]').forEach(function(cb) {{
                if (!cb.checked) uncheckedIndustries.add(cb.dataset.industry);
            }});

            // Build set of hidden link indices (union of cargo-hidden and industry-hidden)
            var hiddenLinks = new Set();
            for (var cargo in sankeyCargoLinkMap) {{
                if (uncheckedCargos.has(cargo)) {{
                    sankeyCargoLinkMap[cargo].forEach(function(idx) {{ hiddenLinks.add(idx); }});
                }}
            }}
            for (var ind in sankeyIndustryLinkMap) {{
                if (uncheckedIndustries.has(ind)) {{
                    sankeyIndustryLinkMap[ind].forEach(function(idx) {{ hiddenLinks.add(idx); }});
                }}
            }}

            // Build new values/colors arrays
            var newValues = sankeyOriginalValues.slice();
            var newColors = sankeyOriginalColors.slice();
            hiddenLinks.forEach(function(idx) {{
                newValues[idx] = 0;
                newColors[idx] = 'rgba(0,0,0,0)';
            }});

            Plotly.restyle(plot, {{
                'link.value': [newValues],
                'link.color': [newColors]
            }}, [0]);
        }}

        function toggleAllGroup(group, state) {{
            var container = group === 'cargo' ? '#cargo-checkboxes' : '#industry-checkboxes';
            document.querySelectorAll(container + ' input[type="checkbox"]').forEach(function(cb) {{
                cb.checked = state;
            }});
            filterSankey();
        }}

        function filterSankeySearch(query) {{
            query = query.toLowerCase().trim();
            document.querySelectorAll('.filter-cb').forEach(function(label) {{
                var text = label.textContent.toLowerCase();
                if (!query || text.includes(query)) {{
                    label.classList.remove('hidden');
                }} else {{
                    label.classList.add('hidden');
                }}
            }});
        }}

        // ── Filter dropdown toggle (click-based) ───────────
        function toggleFilterDropdown(e) {{
            if (e) e.stopPropagation();
            var dd = document.getElementById('sankey-filter-dropdown');
            if (dd) dd.classList.toggle('open');
        }}

        // Close dropdown when clicking outside
        document.addEventListener('click', function(e) {{
            var dd = document.getElementById('sankey-filter-dropdown');
            var wrapper = dd ? dd.closest('.sankey-filter-wrapper') : null;
            if (dd && wrapper && !wrapper.contains(e.target)) {{
                dd.classList.remove('open');
            }}
        }});
    </script>
</body>
</html>"""
    return html


# =====================================================================
# MAIN
# =====================================================================

if __name__ == "__main__":
    print("Building unified NAIS dashboard...")
    print()

    print("  [1/5] Cargo Revenue chart...")
    fig_revenue = apply_theme(build_revenue_figure())

    print("  [2/5] Sankey cargo flow diagram...")
    fig_sankey_raw, sankey_meta = build_sankey_figure()
    fig_sankey = apply_theme(fig_sankey_raw)

    print("  [3/5] Primary production ranges...")
    fig_primary = apply_theme(build_primary_figure())

    print("  [4/5] Secondary conversion heatmap...")
    fig_heatmap = apply_theme(build_heatmap_figure())

    print("  [5/5] Combo boost comparison...")
    fig_combo = apply_theme(build_combo_figure())

    print("  Assembling unified HTML...")
    figures = {
        "revenue":  ("💰 Cargo Revenue",                fig_revenue),
        "sankey":   ("🔀 Cargo Flow (Sankey)",           fig_sankey),
        "primary":  ("📊 Primary Production Ranges",     fig_primary),
        "heatmap":  ("🔥 Secondary Conversion Heatmap",  fig_heatmap),
        "combo":    ("⚡ Combo Boost Comparison",         fig_combo),
    }

    nais_version = get_nais_version()
    commit_info = get_commit_info()
    print(f"  NAIS version: {nais_version}")
    print(f"  NAIS commit:      {commit_info['nais']}")
    print(f"  Dashboard commit: {commit_info['dashboard']}")
    html_content = build_dashboard_html(
        figures, nais_version=nais_version, sankey_meta=sankey_meta, commit_info=commit_info,
    )

    output_file = os.path.join(FIGURES_DIR, "nais_dashboard.html")
    with open(output_file, "w") as f:
        f.write(html_content)

    print(f"\n✅ Dashboard saved to: {output_file}")
