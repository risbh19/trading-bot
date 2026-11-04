"""Bounded polling retries and local structured paper-session health."""

from dataclasses import dataclass
import json
import math
from pathlib import Path
import time

from .recovery import atomic_json


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 3
    base_delay_s: float = 5
    max_delay_s: float = 60

    def __post_init__(self):
        if (type(self.max_retries) is not int or not 0 <= self.max_retries <= 10 or
                type(self.base_delay_s) not in (int, float) or type(self.max_delay_s) not in (int, float) or
                not math.isfinite(self.base_delay_s) or not math.isfinite(self.max_delay_s) or
                not 0 < self.base_delay_s <= self.max_delay_s <= 60):
            raise ValueError('Retries must be 0..10 and backoff must be positive and bounded by 60 seconds')

    def delay(self, consecutive_failures: int, retry_after_s=None):
        if not 1 <= consecutive_failures <= self.max_retries:
            return None
        delay = min(self.max_delay_s, self.base_delay_s * 2 ** (consecutive_failures - 1))
        if retry_after_s is not None:
            if (type(retry_after_s) not in (int, float) or not math.isfinite(retry_after_s)
                    or retry_after_s < 0 or retry_after_s > self.max_delay_s):
                return None  # Stop rather than retry sooner than a longer server delay.
            delay = max(delay, retry_after_s)
        return delay


class SessionHealth:
    """Local files only; counters reset each process, alerts append across resume."""

    def __init__(self, output: Path, symbol: str, session_id: str):
        self.output = output
        self.alerts = (output / 'alerts.jsonl').open('a', encoding='utf-8')
        self.metrics = dict(schema_version=1, mode='paper', symbol=symbol, session_id=session_id,
                            counters_scope='current_process', started_utc_s=time.time(),
                            status='starting', consecutive_failures=0, next_retry_utc_s=None,
                            last_error=None, last_source_time_s=None, last_response_utc_s=None,
                            fetch_attempts=0, responses=0, accepted_quotes=0, duplicate_quotes=0,
                            transient_failures=0, scheduled_retries=0, recoveries=0,
                            validation_failures=0)
        self.last_flush = 0.0

    def transition(self, state: str, event: str, **details):
        if self.metrics['status'] != state:
            self.alerts.write(json.dumps(dict(schema_version=1, time_utc_s=time.time(),
                                              session_id=self.metrics['session_id'],
                                              event=event, status=state, **details), sort_keys=True) + '\n')
            self.alerts.flush()
        self.metrics['status'] = state

    def attempt(self):
        self.metrics['fetch_attempts'] += 1

    def unavailable(self, error, delay):
        self.metrics['transient_failures'] += 1
        self.metrics['consecutive_failures'] += 1
        self.metrics['last_error'] = str(error)
        self.metrics['next_retry_utc_s'] = time.time() + delay if delay is not None else None
        if delay is None:
            self.transition('halted', 'feed_retry_exhausted', error=str(error))
        else:
            self.metrics['scheduled_retries'] += 1
            self.transition('retrying', 'feed_unavailable', error=str(error), retry_delay_s=delay)

    def received(self, quote, changed: bool):
        self.metrics['responses'] += 1
        self.metrics['accepted_quotes' if changed else 'duplicate_quotes'] += 1
        self.metrics['last_response_utc_s'] = time.time()
        self.metrics['last_source_time_s'] = quote.source_time_s
        self.metrics['consecutive_failures'] = 0
        self.metrics['next_retry_utc_s'] = None
        if changed:
            recovering = self.metrics['status'] in ('retrying', 'awaiting_fresh_quote')
            if recovering:
                self.metrics['recoveries'] += 1
            self.transition('healthy', 'feed_recovered' if recovering else 'feed_ready')
            self.metrics['last_error'] = None
        elif self.metrics['status'] == 'retrying':
            self.transition('awaiting_fresh_quote', 'feed_waiting_for_fresh_quote')

    def flush(self, engine_state: dict, *, force=False):
        now = time.monotonic()
        if not force and now - self.last_flush < 5:
            return
        report = dict(self.metrics, updated_utc_s=time.time(),
                      source_age_s=(max(0, time.time() - self.metrics['last_source_time_s'])
                                    if self.metrics['last_source_time_s'] is not None else None),
                      position=engine_state['position'], pnl_paise=engine_state['pnl_paise'],
                      open_orders=engine_state['open_orders'], engine_killed=bool(engine_state['killed']))
        atomic_json(self.output / 'health.json', report)
        self.last_flush = now

    def close(self):
        self.alerts.close()
