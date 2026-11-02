"""Local system telemetry — stdlib only, no subprocess/shell.

Phase 4 backend for GET /api/metrics. Every sampling function takes the
raw text/object it needs (so tests feed fixtures instead of the real
machine) and returns an availability-flagged dict instead of raising:

    {"available": bool, "percent": ...|None, "total_bytes": ...,
     "used_bytes": ..., "available_bytes": ..., "reason": "..."}
"""

import math
import os
import threading
import time

_CPU_PATH = "/proc/stat"
_MEMINFO_PATH = "/proc/meminfo"
_UPTIME_PATH = "/proc/uptime"

# Sampling interval semantics: usage is computed from the difference of
# two consecutive aggregate counter snapshots, which normally happen when
# clients poll (recommended 5-10 s). The exact interval is reported so
# consumers can judge it; very short intervals make the percentage noisy
# but never wrong.
_MIN_CPU_INTERVAL = 0.001


def _clamp_percent(value):
    """Guard the percentage reaching the UI: finite and within [0,100]."""
    if value is None or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return max(0.0, min(100.0, value))


def _unav(fmt, **kw):
    return {"available": False, "reason": fmt.format(**kw)}


def cpu_snapshot(text):
    """Parse the aggregate CPU line of /proc/stat into (idle, total).

    Counters are user/nice/system/idle/iowait/irq/softirq/steal.
    idle+ iowait counts as non-busy (classic Linux accounting); guest
    time is already included in user on Linux and is not double-counted.
    Returns None when the aggregate line is missing or malformed.
    """
    for line in text.splitlines():
        if line.startswith("cpu "):
            parts = line.split()
            try:
                counters = [int(v) for v in parts[1:8]]
            except ValueError:
                return None
            if len(counters) < 5:
                return None
            idle = counters[3] + counters[4]
            total = sum(counters)
            return idle, total
    return None


def cpu_usage(prev, new, interval_seconds):
    """Usage percent between snapshots, or None when not measurable."""
    if not prev or not new:
        return None
    d_total = new[1] - prev[1]
    if d_total <= 0:  # covers zero delta AND counter resets
        return None
    d_idle = new[0] - prev[0]
    if d_idle < 0:
        return None  # idle counters must never go backwards either
    if interval_seconds < _MIN_CPU_INTERVAL:
        return None
    return _clamp_percent((1.0 - d_idle / d_total) * 100.0)


def read_text_file(path, max_bytes=64 * 1024):
    """Read a small virtual file, or None when unreadable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read(max_bytes)
        return data
    except OSError:
        return None


class CpuSampler:
    """Thread-safe single-previous-snapshot calculator.

    Keeps exactly one previous snapshot (bounded state, no history);
    usage is only meaningful between consecutive GET /api/metrics calls.
    """

    def __init__(self, source_path=_CPU_PATH, clock=time.monotonic):
        self._path = source_path
        self._clock = clock
        self._lock = threading.Lock()
        self._prev = None

    def sample(self, read_text=read_text_file):
        """Read counters and derive usage against the previous snapshot.

        The read happens outside the lock, so sampling does not serialize
        unrelated requests; only the tiny snapshot swap is guarded.
        """
        text = read_text(self._path)
        new = cpu_snapshot(text) if text is not None else None
        with self._lock:
            prev = self._prev
            now = self._clock()
            if prev is not None:
                interval = now - prev[0]
                usage = cpu_usage(prev[1], new, interval)
            else:
                usage = None
            if new is not None:
                self._prev = (now, new)
        if new is None:
            return _unav("CPU counters are not readable on this system.")
        if usage is None:
            return {
                "available": False,
                "percent": None,
                "sampling": True,
                "interval_seconds": None,
                "reason": "Need two samples to compute usage; retry shortly.",
            }
        return {
            "available": True,
            "percent": usage,
            "sampling": False,
            "interval_seconds": round(interval, 3),
        }


def read_text_file(path, max_bytes=64 * 1024):
    """Read a small virtual file, or None when unreadable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read(max_bytes)
        return data
    except OSError:
        return None


def memory_payload(text):
    """Parse /proc/meminfo; used = total - available (Linux accounting).

    Reports real bytes (meminfo lines are in kB, i.e. 1024-byte units).
    Missing MemAvailable is an unavailable state — substituting a derived
    guess would present an invented value as a measurement.
    """
    if not isinstance(text, str):
        return _unav("Memory information is not readable on this system.")
    values = {}
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) == 2:
            fields = parts[1].split()
            if fields:
                try:
                    values[parts[0].strip()] = int(fields[0]) * 1024
                except ValueError:
                    pass
    try:
        total = int(values["MemTotal"])
    except (KeyError, ValueError):
        return _unav("Total memory is not reported by /proc/meminfo.")
    if total <= 0:
        return _unav("Total memory is not reported as positive.")
    if "MemAvailable" not in values:
        return _unav(
            "MemAvailable is missing; used memory cannot be measured "
            "honestly on this kernel."
        )
    available = int(values["MemAvailable"])
    used = total - available
    if used < 0 or not math.isfinite(used):
        return _unav("Memory counters are inconsistent.")
    pct = _clamp_percent(used / total * 100.0)
    if pct is None:
        return _unav("Memory counters are inconsistent.")
    return {
        "available": True,
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": available,
        "percent": pct,
    }


def disk_payload(statvfs_result):
    """Turn one os.statvfs result into capacity facts (bytes).

    Scope honesty: the caller measures the filesystem that CONTAINS the
    JARVIS data directory — it is not an "app storage usage" reading.
    """
    if statvfs_result is None:
        return _unav("The data directory's filesystem is not measurable.")
    frsize = statvfs_result.f_frsize or statvfs_result.f_bsize
    total = frsize * statvfs_result.f_blocks
    avail_to_all = frsize * statvfs_result.f_bfree
    avail_user = frsize * statvfs_result.f_bavail
    used = total - avail_to_all
    if total <= 0:
        return _unav("Filesystem reports no capacity.")
    if used < 0 or avail_user < 0 or avail_to_all < 0:
        return _unav("Filesystem counters are inconsistent.")
    pct = _clamp_percent(used / total * 100.0)
    if pct is None:
        return _unav("Filesystem counters are inconsistent.")
    return {
        "available": True,
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": avail_user,
        "percent": pct,
    }


def disk_statvfs(directory):
    """os.statvfs wrapper: None when the path is gone or unusable."""
    try:
        return os.statvfs(str(directory))
    except OSError:
        return None


def uptime_payload(text):
    """Parse /proc/uptime — HOST uptime, unrelated to server uptime."""
    if not isinstance(text, str):
        return _unav("Host uptime is not readable on this system.")
    first = text.split()[0] if text.split() else None
    try:
        seconds = float(first)
    except (TypeError, ValueError):
        return _unav("Host uptime could not be parsed.")
    if not math.isfinite(seconds) or seconds < 0:
        return _unav("Host uptime value is invalid.")
    return {"available": True, "seconds": round(seconds, 3)}


class MetricsService:
    """Standard-library telemetry for one data directory, lock-free per
    caller beyond the CPU sampler's tiny critical section."""

    def __init__(self, data_dir, cpu_sampler):
        self._data_dir = data_dir
        self._cpu = cpu_sampler

    def collect(self, read_text=read_text_file):
        mem = memory_payload(read_text(_MEMINFO_PATH))
        uptime = uptime_payload(read_text(_UPTIME_PATH))
        disk = disk_payload(disk_statvfs(self._data_dir))
        cpu = self._cpu.sample()
        return {
            "sampled_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
            "cpu": dict(cpu, units="percent"),
            "memory": dict(mem),
            "disk": dict(
                disk,
                scope="filesystem containing the JARVIS data directory",
            ),
            "host_uptime": uptime,
            "host_uptime_seconds": (
                uptime.get("seconds") if uptime["available"] else None
            ),
        }
