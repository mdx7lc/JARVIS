"""Phase 4 metrics-sample tests — deterministic fixtures, not this machine.

Covers CPU parsing/calculation (first sample, deltas, zero delta,
malformed input, missing fields, counter resets, thread safety), memory
parsing (bytes/percent, missing/invalid fields), disk via a mocked
os.statvfs, uptime parsing, and the MetricsService assembly (including
per-metric partial failure).
"""

import math
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import metrics


VALID_STAT1 = "cpu  100 0 100 800 0 0 0 0 0 0\n"
VALID_STAT2 = "cpu  200 0 100 900 0 0 0 0 0 0\n"  # +100 busy, +100 idle


class TestCpuParsing(unittest.TestCase):
    def test_valid_snapshot(self):
        snap = metrics.cpu_snapshot(VALID_STAT1)
        self.assertEqual(snap, (800, 1000))  # idle 800, total 1000

    def test_missing_aggregate_line(self):
        self.assertIsNone(metrics.cpu_snapshot("cpu0 1 2 3 4 5\n"))

    def test_malformed_values(self):
        self.assertIsNone(metrics.cpu_snapshot("cpu 1 2 3 x 5\n"))

    def test_too_few_fields(self):
        self.assertIsNone(metrics.cpu_snapshot("cpu 1 2 3\n"))

    def test_calculation_between_two_samples(self):
        # busy grew 100 of 200 total ticks -> 50% busy
        usage = metrics.cpu_usage((800, 1000), (900, 1200), 5.0)
        self.assertEqual(usage, 50.0)

    def test_zero_total_delta_is_not_measurable(self):
        self.assertIsNone(
            metrics.cpu_usage((800, 1000), (800, 1000), 5.0))

    def test_counter_reset_is_not_measurable(self):
        # total went backwards -> cannot trust the delta
        self.assertIsNone(
            metrics.cpu_usage((800, 10000), (900, 1000), 5.0))

    def test_idle_backwards_is_not_measurable(self):
        self.assertIsNone(
            metrics.cpu_usage((900, 1000), (800, 1000), 5.0))

    def test_percent_is_bounded(self):
        # fully-busy interval: idle flat, total grows 400 -> 100%
        busy = metrics.cpu_usage((800, 1000), (800, 1400), 5.0)
        self.assertEqual(busy, 100.0)
        self.assertTrue(0.0 <= busy <= 100.0)

    def test_clamp_rejects_nonfinite(self):
        self.assertIsNone(metrics._clamp_percent(float("nan")))
        self.assertIsNone(metrics._clamp_percent(float("inf")))
        self.assertEqual(metrics._clamp_percent(150.0), 100.0)
        self.assertEqual(metrics._clamp_percent(-3.0), 0.0)


class TestCpuSampler(unittest.TestCase):
    def _rig(self, texts):
        seq = list(texts)
        state = {"i": 0, "t": 0.0}

        def read(_path):
            i = state["i"]
            state["i"] = min(i + 1, len(seq) - 1)
            return seq[i]

        sampler = metrics.CpuSampler(clock=lambda: state["t"])
        return sampler, read, state

    def test_first_sample_is_sampling_state(self):
        sampler, read, state = self._rig([VALID_STAT1])
        payload = sampler.sample(read)
        self.assertFalse(payload["available"])
        self.assertTrue(payload["sampling"])
        self.assertIsNone(payload["percent"])
        self.assertIsNone(payload["interval_seconds"])

    def test_second_sample_reports_usage_and_interval(self):
        sampler, read, state = self._rig([VALID_STAT1, VALID_STAT2])
        self.assertEqual(sampler.sample(read)["sampling"], True)
        state["t"] = 10.0
        payload = sampler.sample(read)
        self.assertTrue(payload["available"])
        self.assertFalse(payload["sampling"])
        self.assertEqual(payload["interval_seconds"], 10.0)

    def test_zero_total_delta_between_calls_is_sampling(self):
        sampler, read, state = self._rig([VALID_STAT1, VALID_STAT1])
        sampler.sample(read)
        state["t"] = 3.0
        payload = sampler.sample(read)
        self.assertFalse(payload["available"])
        self.assertTrue(payload["sampling"])

    def test_busy_delta_only(self):
        # 5s later, only busy counters grew: 100% busy
        sampler, read, state = self._rig(
            [VALID_STAT1, "cpu  200 0 100 800 0 0 0 0\n"])
        sampler.sample(read)
        state["t"] = 7.0
        payload = sampler.sample(read)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["percent"], 100.0)

    def test_unreadable_counters(self):
        sampler = metrics.CpuSampler()
        payload = sampler.sample(lambda _p: None)
        self.assertFalse(payload["available"])
        self.assertIn("not readable", payload["reason"])

    def test_state_stays_bounded_under_load(self):
        sampler, read, _ = self._rig([VALID_STAT1])
        sampler.sample(read)
        for _ in range(200):
            sampler.sample(read)  # same snapshot each time
        with sampler._lock:
            self.assertEqual(sampler._prev[1], (800, 1000))

    def test_concurrent_sampling_no_corruption(self):
        sampler = metrics.CpuSampler()
        text = VALID_STAT1
        errs = []

        def hit():
            try:
                for _ in range(50):
                    sampler.sample(lambda _p: text)
            except Exception as exc:  # pragma: no cover
                errs.append(exc)

        threads = [threading.Thread(target=hit) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errs, [])
        with sampler._lock:
            prev = sampler._prev
        self.assertIsNotNone(prev)
        self.assertEqual(len(prev[1]), 2)  # exactly one snapshot kept


class TestMemoryParsing(unittest.TestCase):
    def test_valid_meminfo_bytes_and_percent(self):
        text = (
            "MemTotal:       1000 kB\n"
            "MemFree:         200 kB\n"
            "MemAvailable:    300 kB\n"
            "Buffers:          50 kB\n"
        )
        payload = metrics.memory_payload(text)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["total_bytes"], 1000 * 1024)
        self.assertEqual(payload["used_bytes"], 700 * 1024)
        self.assertEqual(payload["available_bytes"], 300 * 1024)
        self.assertEqual(payload["percent"], 70.0)

    def test_missing_memavailable_is_unavailable_not_zero(self):
        text = "MemTotal: 1000 kB\nMemFree: 200 kB\n"
        payload = metrics.memory_payload(text)
        self.assertFalse(payload["available"])
        self.assertNotIn("percent", payload)

    def test_missing_total_is_unavailable(self):
        payload = metrics.memory_payload("MemAvailable: 300 kB\n")
        self.assertFalse(payload["available"])

    def test_invalid_total_is_unavailable(self):
        self.assertFalse(
            metrics.memory_payload("MemTotal: xyz kB\n")["available"])

    def test_zero_total_is_unavailable(self):
        self.assertFalse(
            metrics.memory_payload("MemTotal: 0 kB\n")["available"])

    def test_non_string_input_is_unavailable(self):
        self.assertFalse(metrics.memory_payload(None)["available"])

    def test_inconsistent_counters_never_reach_ui(self):
        # available > total would produce negative used memory
        text = "MemTotal: 100 kB\nMemAvailable: 500 kB\n"
        payload = metrics.memory_payload(text)
        self.assertFalse(payload["available"])


class TestDisk(unittest.TestCase):
    def _vfs(self, total=1000, free=500, avail=400, frsize=4096,
             bsize=4096):
        return self._mk(total, free, avail, frsize, bsize)

    @staticmethod
    def _mk(total, free, avail, frsize, bsize):
        class VFS:
            pass
        v = VFS()
        v.f_blocks = total
        v.f_bfree = free
        v.f_bavail = avail
        v.f_frsize = frsize
        v.f_bsize = bsize
        return v

    def test_valid_capacity_and_percent(self):
        payload = metrics.disk_payload(self._vfs())
        self.assertTrue(payload["available"])
        self.assertEqual(payload["total_bytes"], 1000 * 4096)
        # used = total - f_bfree
        self.assertEqual(payload["used_bytes"], 500 * 4096)
        self.assertEqual(payload["available_bytes"], 400 * 4096)
        self.assertEqual(payload["percent"], 50.0)

    def test_zero_total_capacity_is_unavailable(self):
        self.assertFalse(metrics.disk_payload(self._vfs(total=0))
                         ["available"])

    def test_negative_counters_are_unavailable(self):
        self.assertFalse(
            metrics.disk_payload(self._vfs(free=-5))["available"])

    def test_none_is_unavailable(self):
        self.assertFalse(metrics.disk_payload(None)["available"])

    def test_missing_directory_gives_unavailable(self):
        with mock.patch("os.statvfs",
                        side_effect=OSError(2, "No such directory")):
            self.assertIsNone(metrics.disk_statvfs("/definitely/gone"))
        payload = metrics.disk_payload(
            metrics.disk_statvfs("/definitely/gone"))
        self.assertFalse(payload["available"])


class TestUptime(unittest.TestCase):
    def test_valid(self):
        payload = metrics.uptime_payload("12345.67 23456.78\n")
        self.assertTrue(payload["available"])
        self.assertEqual(payload["seconds"], 12345.67)

    def test_malformed(self):
        self.assertFalse(metrics.uptime_payload("abc def\n")["available"])
        self.assertFalse(metrics.uptime_payload("")["available"])
        self.assertFalse(metrics.uptime_payload(None)["available"])

    def test_negative_is_invalid(self):
        self.assertFalse(metrics.uptime_payload("-5.0 5.0\n")["available"])


class TestMetricsService(unittest.TestCase):
    class _FakeVFS:
        f_blocks, f_bfree, f_bavail, f_frsize, f_bsize = 1000, 500, 400, \
            4096, 4096

    def _service(self, data_dir="/srv/jarvis/data"):
        return metrics.MetricsService(
            data_dir=data_dir, cpu_sampler=metrics.CpuSampler())

    def _collect(self, data_dir="/tmp/test-data-dir"):
        with mock.patch.object(metrics, "disk_statvfs",
                               return_value=self._FakeVFS()):
            return self._service(data_dir).collect(
                read_text=lambda p: (
                    "MemTotal: 1000 kB\nMemAvailable: 300 kB\n"
                    if "meminfo" in p else
                    ("cpu  100 0 100 800 0 0 0 0\n" if p.endswith("/stat")
                     else "9.5 5\n")))

    def test_shape_and_units(self):
        payload = self._collect()
        self.assertIn("sampled_at", payload)
        self.assertIn("percent", payload["cpu"])
        self.assertIn("total_bytes", payload["memory"])
        self.assertIn("percent", payload["disk"])
        self.assertIn("seconds", payload["host_uptime"])

    def test_disk_failure_does_not_destroy_other_metrics(self):
        with mock.patch("os.statvfs", side_effect=OSError(13, "denied")):
            payload = self._service().collect(
                read_text=lambda p: "MemTotal: 1000 kB\n"
                "MemAvailable: 300 kB\n" if "meminfo" in p else
                ("cpu  100 0 100 800 0 0 0 0\n" if p.endswith("/stat")
                 else "9.5 5\n"))
        self.assertFalse(payload["disk"]["available"])
        self.assertTrue(payload["memory"]["available"])
        self.assertIn("sampled_at", payload)

    def test_disk_scope_is_not_app_storage(self):
        payload = self._collect()
        self.assertIn("filesystem containing", payload["disk"]["scope"])

    def test_no_absolute_paths_leak(self):
        payload = self._collect(data_dir="/tmp/some/private/dir")
        body = str(payload)
        self.assertNotIn("/tmp/some/private", body)
        self.assertNotIn("/srv", body)


if __name__ == "__main__":
    unittest.main()
