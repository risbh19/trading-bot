'use strict';
(() => {
  const states = new Set(['starting', 'healthy', 'retrying', 'waiting', 'stopped', 'halted', 'unavailable']);
  const labels = {starting: 'Starting', healthy: 'Healthy', retrying: 'Retrying',
    waiting: 'Waiting for fresh data', stopped: 'Stopped', halted: 'Halted', unavailable: 'Unavailable'};
  const explanations = {
    starting: 'The runner is preparing the paper account.',
    healthy: 'A fresh, validated report says the paper account is healthy.',
    retrying: 'A data source is unavailable. The runner is handling its retry policy.',
    waiting: 'The runner is waiting for acceptable fresh source data.',
    stopped: 'The session ended cleanly. Remaining inventory stays marked.',
    halted: 'A failure or risk stop was reported. Check the runner before continuing.',
    unavailable: 'A current report could not be validated. Check the exporter and report directory.'
  };
  const byId = id => document.getElementById(id);
  const money = new Intl.NumberFormat('en-IN', {style: 'currency', currency: 'INR', maximumFractionDigits: 2});
  const count = new Intl.NumberFormat('en-IN', {maximumFractionDigits: 0});
  let last = null, receivedAt = 0, inFlight = false, connected = false;
  let requestGeneration = 0, activeController = null;
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const text = (id, value) => { byId(id).textContent = value; };
  const ageText = value => !finite(value) ? '—' : value < 60 ? Math.floor(value) + 's' :
    value < 3600 ? Math.floor(value / 60) + 'm ' + Math.floor(value % 60) + 's' :
    Math.floor(value / 3600) + 'h ' + Math.floor(value % 3600 / 60) + 'm';
  const dateText = value => finite(value) ? new Date(value * 1000).toLocaleString(undefined,
    {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit'}) : '—';
  function validate(data) {
    if (!data || data.schema_version !== 1 || data.mode !== 'paper' ||
        !['valid', 'fresh', 'healthy'].every(key => typeof data[key] === 'boolean') ||
        !states.has(data.state) || !finite(data.max_age_seconds) || data.max_age_seconds < 5 ||
        data.max_age_seconds > 300 || !Array.isArray(data.sources) || data.sources.length > 32 ||
        (data.valid && (!finite(data.report_age_seconds) || data.report_age_seconds < 0)) ||
        (data.fresh && !data.valid) || (data.healthy && (!data.fresh || data.state !== 'healthy')) ||
        (data.fresh && data.state === 'healthy' && !data.healthy)) {
      throw new Error('Invalid dashboard response');
    }
    for (const source of data.sources) {
      if (!source || typeof source.symbol !== 'string' || !states.has(source.state) ||
          (source.source_age_seconds !== null && (!finite(source.source_age_seconds) || source.source_age_seconds < 0))) {
        throw new Error('Invalid source response');
      }
    }
    return data;
  }
  function clearValues() {
    for (const id of ['equity', 'cash', 'pnl', 'orders', 'fees', 'gross', 'reserved']) text(id, '—');
    byId('pnl').removeAttribute('data-sign');
    text('execution', '—');
  }
  function status(title, detail, tone) {
    text('status-title', title);
    text('status-detail', detail);
    byId('status-panel').dataset.tone = tone;
  }
  function disconnected() {
    last = null;
    connected = false;
    clearValues();
    byId('sources').replaceChildren();
    byId('empty-sources').hidden = false;
    text('empty-sources', 'Source details are unavailable until the connection returns.');
    for (const id of ['report-age', 'freshness', 'updated', 'started']) text(id, '—');
    text('source-count', '0 sources');
    status('Connection lost', 'The dashboard could not fetch a current report. Previous values are hidden.', 'danger');
  }
  function render() {
    if (!last || !connected) return;
    const elapsed = Math.max(0, (performance.now() - receivedAt) / 1000);
    const age = last.valid ? last.report_age_seconds + elapsed : null;
    const fresh = last.valid && last.fresh && age <= last.max_age_seconds;
    clearValues();
    text('report-age', ageText(age));
    text('freshness', ageText(last.max_age_seconds));
    text('updated', dateText(last.updated_utc_s));
    text('started', dateText(last.process_started_utc_s));
    if (!last.valid) status('Report unavailable', explanations.unavailable, 'danger');
    else if (!fresh) status('Report out of date', 'Previous values are hidden. Last reported: ' + labels[last.state] + '.', 'warning');
    else status(labels[last.state], explanations[last.state],
      last.state === 'healthy' ? 'good' : last.state === 'halted' ? 'danger' :
        ['waiting', 'retrying'].includes(last.state) ? 'warning' : 'neutral');
    const account = fresh && last.account && typeof last.account === 'object' ? last.account : {};
    const fields = {equity: 'equity_inr', cash: 'cash_inr', pnl: 'pnl_inr',
      fees: 'fees_inr', gross: 'gross_notional_inr', reserved: 'reserved_cash_inr'};
    for (const [id, key] of Object.entries(fields)) {
      if (finite(account[key])) text(id, money.format(account[key]));
    }
    if (finite(account.open_orders)) text('orders', count.format(account.open_orders));
    if (finite(account.pnl_inr)) byId('pnl').dataset.sign = account.pnl_inr > 0 ? 'positive' : account.pnl_inr < 0 ? 'negative' : 'zero';
    if (fresh && typeof last.execution_enabled === 'boolean') text('execution', last.execution_enabled ? 'Enabled' : 'Paused / stopped');
    else if (fresh) text('execution', 'Not reported');
    const rows = document.createDocumentFragment();
    for (const source of last.sources) {
      const row = document.createElement('tr');
      const counters = fresh && source.counters && typeof source.counters === 'object' ? source.counters : {};
      const values = [source.symbol, labels[source.state], ageText(source.source_age_seconds === null ? null :
        source.source_age_seconds + elapsed), ...['fetch_attempts', 'accepted_quotes', 'failures'].map(
          key => finite(counters[key]) ? count.format(counters[key]) : '—')];
      for (const value of values) {
        const cell = document.createElement('td');
        cell.textContent = value;
        row.appendChild(cell);
      }
      rows.appendChild(row);
    }
    byId('sources').replaceChildren(rows);
    text('source-count', count.format(last.sources.length) + (last.sources.length === 1 ? ' source' : ' sources'));
    byId('empty-sources').hidden = last.sources.length > 0;
    text('empty-sources', 'Source details will appear after a valid report arrives.');
  }
  async function poll() {
    if (inFlight || document.hidden) return;
    inFlight = true;
    const generation = ++requestGeneration;
    const controller = new AbortController();
    activeController = controller;
    const startedAt = performance.now();
    const timeout = setTimeout(() => controller.abort(), 4000);
    byId('refresh').disabled = true;
    try {
      const response = await fetch('/api/snapshot', {signal: controller.signal, cache: 'no-store', credentials: 'omit'});
      if (!response.ok || !(response.headers.get('Content-Type') || '').includes('application/json')) throw new Error('Request failed');
      const data = validate(await response.json());
      if (generation !== requestGeneration) return;
      // Charge the entire request duration against freshness; a delayed response
      // cannot turn an already-expired snapshot healthy again.
      const travel = Math.max(0, (performance.now() - startedAt) / 1000);
      if (data.valid) data.report_age_seconds += travel;
      for (const source of data.sources) if (source.source_age_seconds !== null) source.source_age_seconds += travel;
      last = data;
      connected = true;
      receivedAt = performance.now();
      render();
    } catch (_) {
      if (generation === requestGeneration) disconnected();
    } finally {
      clearTimeout(timeout);
      if (generation === requestGeneration) {
        activeController = null;
        inFlight = false;
        byId('refresh').disabled = false;
      }
    }
  }
  function resume() {
    // Browser suspension can freeze timers and monotonic clocks on some hosts.
    // Require a new response before showing any cached values after restoration.
    ++requestGeneration;
    if (activeController) activeController.abort();
    activeController = null;
    inFlight = false;
    disconnected();
    byId('refresh').disabled = false;
    void poll();
  }
  byId('refresh').addEventListener('click', () => void poll());
  document.addEventListener('visibilitychange', resume);
  window.addEventListener('pageshow', event => { if (event.persisted) resume(); });
  setInterval(() => { if (!document.hidden) render(); }, 250);
  setInterval(() => void poll(), 5000);
  void poll();
})();
