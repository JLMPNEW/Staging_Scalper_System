from __future__ import annotations

import json
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "visualitation" / "Position_Monitor.py"
RUNS = ROOT / "portfolio_layer" / "output" / "runs"


pytestmark = pytest.mark.skipif(
    not RUNS.is_dir(), reason="local sealed portfolio runs are not available"
)


def _element(elements, label: str):
    return next(item for item in elements if item.label == label)


def _plotly_spec_with_traces(app, trace_names: set[str]) -> dict:
    for chart in app.get('plotly_chart'):
        spec = json.loads(chart.proto.spec)
        names = {str(trace.get('name')) for trace in spec.get('data', [])}
        if trace_names.issubset(names):
            return spec
    raise AssertionError(f'No Plotly chart contains traces {sorted(trace_names)}')


def test_dashboard_structure_and_interactive_reruns() -> None:
    streamlit_testing = pytest.importorskip("streamlit.testing.v1")
    app = streamlit_testing.AppTest.from_file(str(APP), default_timeout=180).run()

    performance_spec = _plotly_spec_with_traces(app, {'SPY', 'QQQ'})
    performance_axis = performance_spec['layout']['xaxis']
    assert performance_axis['type'] == 'date'
    assert performance_axis['tickformat'] == '%b %d'
    assert performance_axis['hoverformat'] == '%b %d, %Y'
    assert performance_axis['dtick'] in {86_400_000, 7 * 86_400_000}
    for trace in performance_spec['data']:
        hovertemplate = str(trace.get('hovertemplate', ''))
        assert '%b %d, %Y' in hovertemplate
        assert '%H' not in hovertemplate and '%I' not in hovertemplate

    assert not app.exception
    assert [tab.label for tab in app.tabs] == [
        "Overview",
        "Positions",
        "Index risk",
        "Research queue",
        "Data quality",
        "Sector value · future",
    ]
    metric_labels = {metric.label for metric in app.metric}
    assert {
        "Account value",
        "Realized P&L · MTD",
        "Realized P&L · YTD",
        "Dividends · YTD",
        "Net interest · YTD",
        "H1 estimate · current",
        "H1 estimate · next",
    }.issubset(metric_labels)
    table_markdown = [
        str(element.value) for element in app.markdown
        if str(element.value).startswith('<div class="pc-table-wrap"')
    ]
    rendered_tables = "\n".join(table_markdown)
    assert all(label in rendered_tables for label in [
        "Next earnings", "Market value", "Unrealized P&amp;L", "Target relation", "Next action",
        "Average cost", "Starter low", "Starter high", "Add low", "Add high", "Trim low", "Trim high",
        "Current", "MA50", "MA200", "State", "Pair", "Tactical obs.", "Structural obs.",
    ])
    positions_table = next(table for table in table_markdown if "Market value</th>" in table)
    research_table = next(table for table in table_markdown if "Pipeline</th>" in table)
    assert all(label in positions_table for label in ["Average cost", "Trim low", "Trim high"])
    assert all(label in research_table for label in ["Starter low", "Add high", "State", "Next action"])
    assert "Score</th>" not in rendered_tables
    queue_control = _element(app.radio, "Queue")
    assert "Monitored" not in queue_control.options
    assert "Top 50" in queue_control.options
    assert not any(item.value == "Selected name · execution bands" for item in app.subheader)
    assert not any(item.label == "Ticker" for item in app.selectbox)
    risk_markup = "\n".join(str(element.value) for element in app.markdown)
    assert re.search(r"T \d{1,3}\.\d% · S \d{1,3}\.\d%", risk_markup)
    assert "0.5^(a / 42)" in risk_markup
    assert "not QQQ and SPY" in risk_markup
    assert "Covered portfolio vs QQQ" in risk_markup
    assert "Worked interpretation examples" in risk_markup
    assert "$55,000" in risk_markup
    assert "LHX" in risk_markup and "ISRG" in risk_markup
    example_markup = next(value for value in table_markdown + [
        str(element.value) for element in app.markdown
    ] if 'class="pc-example-grid"' in value)
    assert example_markup.count('class="pc-example-card"') == 4
    assert all(label in example_markup for label in [
        "Portfolio ladder + SPY benchmark detail", "LHX → XAR", "ISRG → IHI",
        "Illustrative hedge workflow", "Fact:", "PM read:", "Boundary:", " pp",
    ])
    assert "recent diversification improved" not in example_markup
    assert "%" in str(_element(app.metric, "Dominant index").delta)
    assert _element(app.metric, "Covered portfolio vs SPY").value.endswith("%")
    benchmark_detail = _element(app.selectbox, "Benchmark detail")
    assert benchmark_detail.value == "SPY"
    matrix_spec = json.loads(app.get("plotly_chart")[-1].proto.spec)
    heatmap = matrix_spec["data"][0]
    assert heatmap["textfont"]["size"] >= 12
    assert all(str(value).endswith("%") for row in heatmap["text"] for value in row)
    assert heatmap["colorbar"]["tickformat"] == ".0%"

    _element(app.radio, "Performance period").set_value("YTD")
    app.run()
    assert not app.exception
    ytd_performance_spec = _plotly_spec_with_traces(app, {'SPY', 'QQQ'})
    ytd_axis = ytd_performance_spec['layout']['xaxis']
    assert ytd_axis['type'] == 'date'
    assert ytd_axis['tickformat'] == '%b'
    assert ytd_axis['hoverformat'] == '%b %d, %Y'
    assert ytd_axis['dtick'] == 'M1'
    ytd = _element(app.metric, "Portfolio · YTD")
    assert ytd.value == "Unavailable"
    assert _element(app.metric, "Active vs SPY").value == "Unavailable"
    assert _element(app.metric, "SPY").value != "n/a"
    warnings = "\n".join(str(item.value) for item in app.warning)
    assert "IB daily TWR coverage is 89 of 177 required weekdays" in warnings
    assert "88 dates are missing" in warnings
    assert "Jan 26, 2026 to Feb 20, 2026" in warnings
    assert "YTD dollar P&L" in warnings

    _element(app.radio, "ETF matrix horizon").set_value(250)
    app.run()
    assert not app.exception

    _element(app.radio, "Queue").set_value("Top 50")
    app.run()
    assert not app.exception
    top_50_tables = [
        str(element.value) for element in app.markdown
        if str(element.value).startswith('<div class="pc-table-wrap"')
        and "Pipeline</th>" in str(element.value)
    ]
    assert len(top_50_tables) == 1
    assert top_50_tables[0].count("<tr") - 1 <= 50
    assert "Score</th>" not in top_50_tables[0]

    _element(app.radio, "Queue").set_value("Held only")
    app.run()
    assert not app.exception
    assert not any(item.label == "Ticker" for item in app.selectbox)


def test_requested_september_third_snapshot_and_no_duplicate_page() -> None:
    streamlit_testing = pytest.importorskip("streamlit.testing.v1")
    app = streamlit_testing.AppTest.from_file(str(APP), default_timeout=180).run()
    selector = _element(app.selectbox, "As-of date")
    if "2026-09-03" not in selector.options:
        pytest.skip("The requested 2026-09-03 sealed run is not present")
    selector.set_value("2026-09-03")
    app.run()
    assert not app.exception
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Realized P&L · MTD"] == "$843.47"
    assert metrics["Realized P&L · YTD"] == "$59,517.96"
    assert metrics["Dividends · YTD"] == "$1,914.24"
    assert metrics["Net interest · YTD"] == "$2,765.40"
    assert metrics["Holding coverage"] == "60.1%"
    assert not (ROOT / "visualitation" / "pages" / "2_Index_Correlations.py").exists()
