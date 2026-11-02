"""Phase 4 frontend dashboard checks — source-level assertions.

Browser automation is not available in this environment; behaviour is
verified by HTTP integration tests + source inspection. A manual
browser pass remains outstanding (documented in the docs/audit).
"""

import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
STYLES_CSS = (STATIC / "styles.css").read_text(encoding="utf-8")


class TestMetricsWiring(unittest.TestCase):
    def test_metric_column_reads_api(self):
        self.assertIn("fetch('/api/metrics'", APP_JS)
        self.assertIn("async function refreshMetrics", APP_JS)

    def test_single_flight_poll(self):
        self.assertIn("if (metricsInFlight) return;", APP_JS)
        self.assertIn("metricsInFlight = true;", APP_JS)

    def test_stale_response_guard(self):
        self.assertIn("if (seq !== gaugeSeq) return;", APP_JS)

    def test_one_poll_loop_created_once(self):
        self.assertIn("function startDashboardRefresh() {", APP_JS)
        self.assertLessEqual(
            APP_JS.count("setInterval("), 4,  # clock + metrics + task loop
            "poll timers must stay bounded",
        )
        init_at = APP_JS.index("function init() {")
        self.assertLess(
            APP_JS.index("startDashboardRefresh();", init_at),
            APP_JS.index("document.addEventListener('keydown'", init_at),
            "dashboard refresh must start once during init",
        )

    def test_visibility_pause_and_resume(self):
        self.assertIn("if (!document.hidden) refreshMetrics();",
                      APP_JS, "pause polling while the tab is hidden")
        self.assertIn("document.addEventListener('visibilitychange'", APP_JS)

    def test_synth_generation_removed(self):
        self.assertNotIn("const GAUGES = [", APP_JS)
        self.assertNotIn("value: 15 }", APP_JS)
        self.assertNotIn("value: 54 }", APP_JS)
        self.assertNotIn("value: 40 }", APP_JS)
        self.assertNotIn("Demo data</span>", APP_JS)
        self.assertNotIn("demo data\">", APP_JS)

    def test_honest_gauge_states_exist(self):
        self.assertIn("Sampling…", APP_JS)  # cpu first sample
        self.assertIn("'Server unreachable'", APP_JS)
        self.assertIn("'Unavailable'", APP_JS)
        self.assertIn("Waiting for server…", APP_JS)

    def test_values_clamped_before_dom(self):
        self.assertIn("Math.max(0, Math.min(100, section.percent))", APP_JS)
        self.assertIn("Number.isFinite(section.percent)", APP_JS)

    def test_no_random_replacement_after_failure(self):
        # no Math.random inside the gauges section (visualizations keep
        # their deterministic PRNG, unrelated to gauges)
        gauges = APP_JS.split("GAUGES", 1)[1].split("VISUALIZATIONS", 1)[0]
        self.assertNotIn("Math.random", gauges)

    def test_monitor_chip_truthful(self):
        self.assertIn('id="h-monitor-chip"', INDEX_HTML)
        self.assertNotIn('<span class="chip chip-demo">Demo data</span>',
                         INDEX_HTML)
        self.assertIn("applyMonitorChip", APP_JS)

    def test_unavail_state_css_existing(self):
        self.assertIn(".gauge.is-unavail", STYLES_CSS)
        self.assertIn(".gauge.is-sampling", STYLES_CSS)


class TestOverviewData(unittest.TestCase):
    def test_telemetry_goes_to_diagnostics_only(self):
        # Phase 11B: the System overview row is gone; live telemetry lives
        # in the Diagnostics view (gauges + monitor chip), never on Home.
        self.assertIn("applyMonitorChip", APP_JS)
        self.assertIn('id="view-diagnostics"', INDEX_HTML)
        self.assertIn('id="gauges"', INDEX_HTML)
        self.assertNotIn("value: 'Browser sandbox'", APP_JS)
        self.assertNotIn("status: 'Stable', sub: 'demo harness'", APP_JS)

    def test_exact_task_counts_used(self):
        self.assertIn("state.statusCounts", APP_JS)
        self.assertIn("state.dayCounts", APP_JS)
        self.assertIn("data.status_counts", APP_JS)
        self.assertIn("data.day_counts", APP_JS)

    def test_tasks_revision_guard_against_stale_overwrite(self):
        self.assertIn("state.tasksRevision += 1;", APP_JS)
        self.assertIn("state.tasksRevision !== revisionAtFetch", APP_JS)


class TestBriefingData(unittest.TestCase):
    def test_no_fabricated_monitor_values(self):
        self.assertNotIn("CPU 15% · RAM 54% · Disk 40%", APP_JS)
        self.assertNotIn("fixed demo values, not live telemetry", APP_JS)

    def test_provider_config_not_conflated_with_verified(self):
        self.assertIn(
            "configuration only, not a verified upstream connection",
            APP_JS)

    def test_unavailable_metrics_stated(self):
        self.assertIn("unreachable — showing no numbers", APP_JS)
        self.assertIn("sampling…", APP_JS.lower())

    def test_briefing_uses_real_status_contract(self):
        self.assertIn("state.lastStatus", APP_JS)
        self.assertIn("s.db === 'ready'", APP_JS)

    def test_stale_status_marked(self):
        self.assertIn("Last known application status", APP_JS)
        self.assertIn("state.lastStatusError", APP_JS)

    def test_briefing_not_ai_generated(self):
        self.assertIn("Not AI-generated", APP_JS)
        self.assertIn("not AI-generated", INDEX_HTML)

    def test_briefing_chip_updated(self):
        self.assertNotIn("Visible demo data only", INDEX_HTML)


class TestDemoSurfacesRemoved(unittest.TestCase):
    """Phase 11B removed the fabricated panels; these assert they stay gone."""

    def test_feed_memory_placeholder_counts_gone(self):
        self.assertNotIn("fictional placeholder counts", APP_JS)

    def test_agent_roster_gone(self):
        self.assertNotIn("None can execute work", APP_JS)

    def test_voice_mic_button_gone(self):
        self.assertNotIn('id="qc-voice"', INDEX_HTML)
        # the honest not-configured answer stays in the scripted fallback
        self.assertIn("Voice is not implemented in this build", APP_JS)

    def test_chat_logic_untouched_markers(self):
        self.assertIn("function mockReply", APP_JS)
        self.assertIn("restoreComposerFocus", APP_JS)

    def test_format_renderer_untouched(self):
        renderer = (STATIC / "jarvis-format.js").read_text(
            encoding="utf-8")
        self.assertFalse("Phase 4" in renderer)


if __name__ == "__main__":
    unittest.main()
