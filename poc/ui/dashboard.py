"""
NOC Dashboard. Polls PostgreSQL for new alerts, displays them with severity
color coding, and lets operators classify each alert with one click.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime

import psycopg2
import psycopg2.extensions
import psycopg2.extras
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from alerts.engine import apply_label, clear_all_suppressions
from alerts.models import Label

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# Human-friendly names for config types shown in the UI
_CONFIG_TYPE_DISPLAY_NAMES: dict[str, str] = {
    "dangerous_program_configuration": "Dangerous Programs",
    "communication_configuration": "Communications",
    "persistence_configuration": "Persistence",
    "capability_configuration": "Capabilities",
    "evasion_configuration": "Evasion",
}

_SEVERITY_COLORS: dict[str, str] = {
    "HIGH": "#dc2626",
    "MEDIUM": "#d97706",
    "LOW": "#ca8a04",
}

_SEVERITY_BACKGROUND_COLORS: dict[str, str] = {
    "HIGH": "rgba(220, 38, 38, 0.08)",
    "MEDIUM": "rgba(217, 119, 6, 0.06)",
    "LOW": "rgba(202, 138, 4, 0.04)",
}

_LABEL_BUTTON_STYLES: dict[str, dict[str, str]] = {
    "true_positive": {"label": "True Positive", "icon": "target"},
    "false_positive": {"label": "False Positive", "icon": "x-circle"},
    "expected_deviation": {"label": "Expected", "icon": "check-circle"},
}


def get_database_connection() -> psycopg2.extensions.connection:
    """Return a cached database connection, reconnecting if it was closed."""
    connection = st.session_state.get("database_connection")
    if connection is None or connection.closed:
        logger.info("Establishing new PostgreSQL connection to %s", config.DATABASE_DSN.split("@")[-1])
        connection = psycopg2.connect(config.DATABASE_DSN)
        st.session_state["database_connection"] = connection
    return connection


def fetch_alerts(
    database_connection: psycopg2.extensions.connection,
    severity_filter: str | None = None,
    group_filter: str | None = None,
) -> list[dict]:
    """Fetch alerts with optional severity and group filters."""
    conditions = []
    parameters: list = []

    if severity_filter and severity_filter != "All":
        conditions.append("severity = %s")
        parameters.append(severity_filter)
    if group_filter and group_filter != "All":
        conditions.append("group_id = %s")
        parameters.append(group_filter)

    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

    with database_connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            f"SELECT * FROM alerts {where_clause} ORDER BY timestamp DESC LIMIT %s",
            parameters + [config.DASHBOARD_ALERT_LIMIT],
        )
        alerts = [dict(row) for row in cursor.fetchall()]
    logger.debug(
        "Fetched %d alerts (severity=%s, group=%s)",
        len(alerts), severity_filter or "all", group_filter or "all",
    )
    return alerts


def fetch_label_metrics(database_connection: psycopg2.extensions.connection) -> dict:
    with database_connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT
                COUNT(*)                                              AS total_alerts,
                COUNT(*) FILTER (WHERE severity = 'HIGH')             AS high_count,
                COUNT(*) FILTER (WHERE severity = 'MEDIUM')           AS medium_count,
                COUNT(*) FILTER (WHERE severity = 'LOW')              AS low_count,
                COUNT(*) FILTER (WHERE label = 'true_positive')       AS true_positive_count,
                COUNT(*) FILTER (WHERE label = 'false_positive')      AS false_positive_count,
                COUNT(*) FILTER (WHERE label = 'expected_deviation')  AS expected_deviation_count,
                COUNT(*) FILTER (WHERE label IS NULL)                 AS unlabeled_count
            FROM alerts
            """
        )
        return dict(cursor.fetchone())


def fetch_detection_rate(database_connection: psycopg2.extensions.connection) -> dict:
    """
    Detection rate = alerts that correctly covered an injected anomaly
    / total injected anomalies.
    """
    with database_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                COUNT(DISTINCT t.id) FILTER (
                    WHERE t.is_anomaly = TRUE AND a.alert_id IS NOT NULL
                )                                                       AS detected,
                COUNT(DISTINCT t.id) FILTER (WHERE t.is_anomaly = TRUE) AS total_injected
            FROM telemetry t
            LEFT JOIN alerts a
                ON  t.implant_id  = a.implant_id
                AND t.config_type = a.config_type
                AND t.ingested_at >= a.window_start
                AND t.ingested_at <  a.window_end
            """
        )
        row = cursor.fetchone()
        if row is None:
            return {"detected": 0, "total_injected": 0}
        return {"detected": row[0], "total_injected": row[1]}


def fetch_suppressed_patterns(database_connection: psycopg2.extensions.connection) -> list[dict]:
    with database_connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            "SELECT * FROM suppressed_patterns ORDER BY suppressed_at DESC"
        )
        return [dict(row) for row in cursor.fetchall()]


def fetch_available_groups(database_connection: psycopg2.extensions.connection) -> list[str]:
    """Return all group IDs that have generated alerts."""
    with database_connection.cursor() as cursor:
        cursor.execute("SELECT DISTINCT group_id FROM alerts ORDER BY group_id")
        return [row[0] for row in cursor.fetchall()]


def fetch_raw_telemetry(
    database_connection: psycopg2.extensions.connection,
    implant_id: str,
    config_type: str,
    window_start: datetime,
    window_end: datetime,
) -> dict | None:
    """Fetch the most recent raw telemetry for an alert's implant and config type.
    Tries ingested_at first (matches detector's scoring window), then falls back
    to the most recent row for that implant+config_type combination."""
    with database_connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT raw FROM telemetry
            WHERE implant_id = %s
              AND config_type = %s
              AND ingested_at >= %s
              AND ingested_at <  %s
            ORDER BY ingested_at DESC
            LIMIT 1
            """,
            (implant_id, config_type, window_start, window_end),
        )
        row = cursor.fetchone()
        if row is not None:
            return dict(row)["raw"]

        cursor.execute(
            """
            SELECT raw FROM telemetry
            WHERE implant_id = %s AND config_type = %s
            ORDER BY ingested_at DESC
            LIMIT 1
            """,
            (implant_id, config_type),
        )
        row = cursor.fetchone()
        return dict(row)["raw"] if row else None


def display_config_type(config_type: str) -> str:
    """Return a human-friendly display name for a config type."""
    return _CONFIG_TYPE_DISPLAY_NAMES.get(config_type, config_type)


def render_severity_badge(severity: str) -> str:
    color = _SEVERITY_COLORS.get(severity, "#666666")
    return (
        f'<span style="background:{color};color:white;padding:3px 12px;'
        f'border-radius:12px;font-weight:600;font-size:0.8em;'
        f'letter-spacing:0.05em">{severity}</span>'
    )


def _card_background_html(severity: str) -> str:
    background_color = _SEVERITY_BACKGROUND_COLORS.get(severity, "transparent")
    border_color = _SEVERITY_COLORS.get(severity, "#666")
    return (
        f'<div style="background:{background_color};border-radius:8px;'
        f'padding:12px 16px;margin-bottom:4px;'
        f'border-left:4px solid {border_color}">'
    )


def _render_card_header(alert: dict) -> None:
    header_column, badge_column = st.columns([5, 1])
    with header_column:
        st.markdown(
            f"**{alert['implant_id']}** &nbsp;\u00b7&nbsp; "
            f"group `{alert['group_id']}` &nbsp;\u00b7&nbsp; "
            f"`{display_config_type(alert['config_type'])}`"
        )
    with badge_column:
        st.markdown(render_severity_badge(alert["severity"]), unsafe_allow_html=True)


def _render_card_details(alert: dict) -> None:
    st.caption(alert["explanation"])
    details = alert.get("details")
    if isinstance(details, dict):
        st.caption(
            f"Window: {alert['window_start']} \u2192 {alert['window_end']} "
            f"| Baseline: {alert['baseline_used']} "
            f"| IF score: {details.get('isolation_forest_score', 0):.3f}"
        )
    else:
        st.caption(f"Window: {alert['window_start']} \u2192 {alert['window_end']}")


_LABEL_BUTTONS = [
    (":dart: True Positive", "true_positive", Label.TRUE_POSITIVE),
    (":x: False Positive", "false_positive", Label.FALSE_POSITIVE),
    (":white_check_mark: Expected", "expected", Label.EXPECTED_DEVIATION),
]


def _render_label_button(
    text: str, key_prefix: str, label: Label,
    alert_id: str, database_connection: psycopg2.extensions.connection,
) -> None:
    if st.button(text, key=f"{key_prefix}_{alert_id}", use_container_width=True):
        try:
            apply_label(database_connection, alert_id, label)
            st.rerun()
        except Exception as exception:
            st.error(f"Failed to save label: {exception}")


def _render_label_buttons(alert_id: str, database_connection: psycopg2.extensions.connection) -> None:
    columns = st.columns([1, 1, 1, 2])
    for column, (text, key_prefix, label) in zip(columns, _LABEL_BUTTONS):
        with column:
            _render_label_button(text, key_prefix, label, alert_id, database_connection)


def _render_raw_telemetry_expander(alert: dict, database_connection: psycopg2.extensions.connection) -> None:
    with st.expander("View raw telemetry"):
        raw = fetch_raw_telemetry(
            database_connection, alert["implant_id"], alert["config_type"],
            alert["window_start"], alert["window_end"],
        )
        if raw is not None:
            st.json(raw if isinstance(raw, (dict, list)) else json.loads(raw))
        else:
            st.caption("No matching telemetry row found.")


def render_alert_card(
    alert: dict,
    database_connection: psycopg2.extensions.connection,
) -> None:
    with st.container():
        st.markdown(_card_background_html(alert["severity"]), unsafe_allow_html=True)
        _render_card_header(alert)
        _render_card_details(alert)

        if alert.get("label"):
            st.success(f"Labeled: **{alert['label'].replace('_', ' ').title()}**")
        else:
            _render_label_buttons(str(alert["alert_id"]), database_connection)

        _render_raw_telemetry_expander(alert, database_connection)
        st.markdown("</div>", unsafe_allow_html=True)
    st.divider()


def _severity_card_html(count: int, label: str, color: str) -> str:
    """Generate HTML for a single severity count card."""
    return (
        f'<div style="text-align:center;padding:8px;background:{color}11;'
        f'border-radius:8px"><div style="font-size:1.8em;font-weight:700;'
        f'color:{color}">{count}</div>'
        f'<div style="font-size:0.85em;color:#888">{label}</div></div>'
    )


def render_severity_breakdown(metrics: dict) -> None:
    """Show severity counts as colored metric cards."""
    cards = [
        (metrics["high_count"], "HIGH", _SEVERITY_COLORS["HIGH"]),
        (metrics["medium_count"], "MEDIUM", _SEVERITY_COLORS["MEDIUM"]),
        (metrics["low_count"], "LOW", _SEVERITY_COLORS["LOW"]),
    ]
    for column, (count, label, color) in zip(st.columns(3), cards):
        with column:
            st.markdown(_severity_card_html(count, label, color), unsafe_allow_html=True)


def render_metrics_panel(
    metrics: dict,
    detection: dict,
    suppressed_patterns: list,
    database_connection: psycopg2.extensions.connection,
) -> None:
    st.subheader("Detection Summary")
    render_severity_breakdown(metrics)
    st.markdown("---")
    _render_detection_rates(metrics, detection)
    st.markdown("---")
    _render_label_breakdown(metrics, suppressed_patterns, database_connection)


def _render_detection_rates(metrics: dict, detection: dict) -> None:
    labeled_count = metrics["true_positive_count"] + metrics["false_positive_count"]
    false_positive_rate = (
        metrics["false_positive_count"] / labeled_count if labeled_count > 0 else 0.0
    )
    total_injected = detection["total_injected"]
    detection_rate = (
        detection["detected"] / total_injected if total_injected > 0 else 0.0
    )
    left_column, right_column = st.columns(2)
    with left_column:
        st.metric("Detection Rate", f"{detection_rate:.1%}")
        st.caption(f"{detection['detected']} / {total_injected} anomalies caught")
    with right_column:
        st.metric("False Positive Rate", f"{false_positive_rate:.1%}")
        st.caption(f"{metrics['false_positive_count']} / {labeled_count} labeled")


def _render_label_breakdown(
    metrics: dict,
    suppressed_patterns: list,
    database_connection: psycopg2.extensions.connection,
) -> None:
    st.markdown("**Label Breakdown**")
    left_column, right_column = st.columns(2)
    with left_column:
        st.metric("True Positives", metrics["true_positive_count"])
        st.metric("False Positives", metrics["false_positive_count"])
    with right_column:
        st.metric("Expected Deviations", metrics["expected_deviation_count"])
        st.metric("Unlabeled", metrics["unlabeled_count"])

    if suppressed_patterns:
        st.markdown("---")
        with st.expander(f"Suppressed Patterns ({len(suppressed_patterns)})"):
            for pattern in suppressed_patterns:
                st.text(
                    f"{pattern['pattern_hash']} "
                    f"\u2014 suppressed {pattern['suppressed_at']} "
                    f"({pattern['label_count']} labels)"
                )
            if st.button("Clear All Suppressions", key="clear_suppressions"):
                try:
                    cleared = clear_all_suppressions(database_connection)
                    st.success(f"Cleared {cleared} suppressed patterns")
                    st.rerun()
                except Exception as exception:
                    st.error(f"Failed to clear suppressions: {exception}")


def render_filter_sidebar(
    database_connection: psycopg2.extensions.connection,
) -> tuple[str | None, str | None]:
    """Render filter controls in the sidebar and return selected filters."""
    with st.sidebar:
        st.header("Filters")

        severity_filter = st.selectbox(
            "Severity",
            options=["All", "HIGH", "MEDIUM", "LOW"],
            index=0,
        )

        available_groups = fetch_available_groups(database_connection)
        group_options = ["All"] + available_groups
        group_filter = st.selectbox(
            "Group",
            options=group_options,
            index=0,
        )

        st.markdown("---")
        st.caption(
            f"Auto-refreshing every {config.DASHBOARD_REFRESH_INTERVAL_SECONDS}s"
        )

    return severity_filter, group_filter


def build_filter_label(severity_filter: str | None, group_filter: str | None) -> str:
    """Build a human-readable label describing the active filters."""
    parts = []
    if severity_filter and severity_filter != "All":
        parts.append(severity_filter)
    if group_filter and group_filter != "All":
        parts.append(f"group {group_filter}")
    return f" \u2014 {', '.join(parts)}" if parts else ""


def render_alert_feed(
    alerts: list[dict],
    database_connection: psycopg2.extensions.connection,
    filter_label: str,
) -> None:
    """Render the scrollable alert feed in the left column."""
    st.subheader(f"Alert Feed ({len(alerts)} alerts{filter_label})")

    if not alerts:
        message = (
            "No alerts match the current filters." if filter_label
            else "No alerts yet. Alerts will appear here as the "
            "detector processes incoming telemetry."
        )
        st.info(message)
        return

    for alert in alerts:
        if isinstance(alert.get("details"), str):
            alert["details"] = json.loads(alert["details"])
        render_alert_card(alert, database_connection)


def main() -> None:
    st.set_page_config(
        page_title="Implant NOC Dashboard",
        layout="wide",
        page_icon="radar",
    )
    st.title("Implant Anomaly Detection \u2014 NOC Dashboard")

    database_connection = get_database_connection()
    severity_filter, group_filter = render_filter_sidebar(database_connection)

    logger.debug("Refreshing dashboard data (severity=%s, group=%s)",
                 severity_filter or "all", group_filter or "all")
    alerts = fetch_alerts(database_connection, severity_filter, group_filter)
    metrics = fetch_label_metrics(database_connection)
    detection = fetch_detection_rate(database_connection)
    suppressed_patterns = fetch_suppressed_patterns(database_connection)
    logger.debug(
        "Dashboard data loaded: %d alerts, %d total, detection rate %d/%d, %d suppressed",
        len(alerts), metrics["total_alerts"],
        detection["detected"], detection["total_injected"],
        len(suppressed_patterns),
    )

    alert_feed_column, metrics_column = st.columns([2, 1])
    with metrics_column:
        render_metrics_panel(metrics, detection, suppressed_patterns, database_connection)
    with alert_feed_column:
        filter_label = build_filter_label(severity_filter, group_filter)
        render_alert_feed(alerts, database_connection, filter_label)

    time.sleep(config.DASHBOARD_REFRESH_INTERVAL_SECONDS)
    st.rerun()


if __name__ == "__main__":
    main()
