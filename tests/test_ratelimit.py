"""Phase 6A rate limiter unit tests — fake clock, no sleeps, no server."""

import threading
import unittest

from ratelimit import RateLimiter


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestFixedWindowBasics(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.limiter = RateLimiter(
            {"chat": (3, 60.0)}, clock=self.clock)

    def test_under_limit_succeeds(self):
        for _ in range(3):
            allowed, retry = self.limiter.hit("chat")
            self.assertTrue(allowed)
            self.assertEqual(retry, 0)

    def test_over_limit_returns_429_style_retry(self):
        for _ in range(3):
            self.limiter.hit("chat")
        allowed, retry = self.limiter.hit("chat")
        self.assertFalse(allowed)
        self.assertTrue(1 <= retry <= 60, retry)

    def test_retry_counts_down_to_one(self):
        for _ in range(3):
            self.limiter.hit("chat")
        allowed, retry = self.limiter.hit("chat")  # depleted
        self.assertFalse(allowed)
        self.assertTrue(1 <= retry <= 60)
        # just inside the window: retry shrinks as time passes
        self.clock.advance(retry - 1)
        self.assertEqual(self.limiter.hit("chat")[1], 1)

    def test_window_reset_after_elapsed_interval(self):
        for _ in range(3):
            self.limiter.hit("chat")
        self.assertFalse(self.limiter.hit("chat")[0])  # blocked at 4th
        self.clock.advance(60.5)
        allowed, _ = self.limiter.hit("chat")
        self.assertTrue(allowed)

    def test_policies_do_not_interfere(self):
        self.limiter.add_policy("search", 1, 60.0)
        self.assertTrue(self.limiter.hit("chat")[0])
        self.assertTrue(self.limiter.hit("search")[0])
        # chat still has capacity left, search was a 1-per-window policy
        self.assertTrue(self.limiter.hit("chat")[0])
        self.assertFalse(self.limiter.hit("search")[0])

    def test_unknown_policy_is_explicit(self):
        with self.assertRaises(KeyError):
            self.limiter.hit("nope")

    def test_zero_limit_blocks_immediately(self):
        self.limiter.add_policy("blocked", 0, 60.0)
        self.assertFalse(self.limiter.hit("blocked")[0])

    def test_reset_clears_counters(self):
        for _ in range(3):
            self.limiter.hit("chat")
        self.limiter.reset()
        self.assertTrue(self.limiter.hit("chat")[0])


class TestConcurrencyAndHashCode(unittest.TestCase):
    def test_threads_cannot_bypass_the_limit(self):
        limiter = RateLimiter({"chat": (10, 60.0)})
        allowed_count = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()  # maximize the race window
            for _ in range(50):
                allowed, _ = limiter.hit("chat")
                with lock:
                    allowed_count.append(1 if allowed else 0)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        accepted = sum(allowed_count)
        self.assertEqual(len(allowed_count), 400)
        self.assertEqual(accepted, 10, "races must not exceed the limit")

    def test_bucket_map_stays_bounded(self):
        limiter = RateLimiter(
            {"chat": (5, 60.0)}, max_buckets=16)
        for i in range(500):
            self.limiter_none_check(limiter, i)
        self.assertLessEqual(len(limiter._buckets), 16)

    @staticmethod
    def limiter_none_check(limiter, i):
        limiter.hit("chat", key=f"client-{i}")


if __name__ == "__main__":
    unittest.main()
