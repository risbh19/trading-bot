"""Offline retry budgets and local health-transition reporting."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live.feeds import Quote, QuoteFeedUnavailable
from live.health import RetryPolicy, SessionHealth


class HealthTests(unittest.TestCase):
    def test_retry_budget_backoff_and_server_delay(self):
        policy = RetryPolicy()
        self.assertEqual([policy.delay(n) for n in range(1, 5)], [5, 10, 20, None])
        self.assertEqual(RetryPolicy(max_retries=10).delay(10), 60)
        self.assertEqual(policy.delay(1, 30), 30)
        for delay in (120, float('nan'), float('inf'), -1, True):
            with self.subTest(delay=delay):
                self.assertIsNone(policy.delay(1, delay))
        self.assertIsNone(RetryPolicy(max_retries=0).delay(1))
        for args in (dict(max_retries=11), dict(max_retries=True), dict(base_delay_s=0),
                     dict(base_delay_s=float('nan')), dict(base_delay_s=True)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                RetryPolicy(**args)

    def test_repeated_failures_alert_once_and_duplicate_does_not_mark_recovery(self):
        state = dict(position=0, pnl_paise=0, open_orders=0, killed=0)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            health = SessionHealth(root, 'TEST.NS', 'test-session')
            try:
                health.attempt()
                health.received(Quote('TEST.NS', 10000, 1000), True)
                error = QuoteFeedUnavailable('test outage')
                health.unavailable(error, 5)
                health.unavailable(error, 10)
                health.received(Quote('TEST.NS', 10000, 1000), False)
                self.assertEqual(health.metrics['status'], 'awaiting_fresh_quote')
                self.assertEqual(health.metrics['recoveries'], 0)
                health.received(Quote('TEST.NS', 10001, 1060), True)
                health.flush(state, force=True)
                metrics = json.loads((root / 'health.json').read_text())
                alerts = [json.loads(line) for line in (root / 'alerts.jsonl').read_text().splitlines()]
                self.assertEqual(metrics['status'], 'healthy')
                self.assertEqual(metrics['transient_failures'], 2)
                self.assertEqual(metrics['scheduled_retries'], 2)
                self.assertEqual(metrics['duplicate_quotes'], 1)
                self.assertEqual(metrics['recoveries'], 1)
                self.assertEqual(metrics['counters_scope'], 'current_process')
                self.assertEqual([row['event'] for row in alerts].count('feed_unavailable'), 1)
                self.assertEqual([row['event'] for row in alerts].count('feed_recovered'), 1)
            finally:
                health.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
