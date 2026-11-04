// Exercise the shipped browser code in a real, isolated headless Chrome.
// Only the snapshot response is a fixture; HTML/CSS/JS are the shipped assets.
import assert from 'node:assert/strict';
import {spawn, spawnSync} from 'node:child_process';
import {createServer} from 'node:http';
import {mkdtemp, readFile, writeFile, mkdir, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve, dirname, basename} from 'node:path';
import {fileURLToPath} from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const candidates = [process.env.CHROME_BIN, 'google-chrome', 'chromium', 'chromium-browser',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'].filter(Boolean);
const chromePath = candidates.find(candidate => spawnSync(candidate, ['--version'], {windowsHide: true}).status === 0);
assert.ok(chromePath, 'Install Chrome/Chromium or set CHROME_BIN for the browser check');
const tempParent = resolve(process.env.DASH_TEST_TMP || tmpdir());
const temporary = await mkdtemp(join(tempParent, 'paper-dashboard-'));
const profile = join(temporary, 'profile');
await mkdir(profile);
const screenshotDir = process.env.DASH_SCREENSHOT_DIR;
if (screenshotDir) await mkdir(screenshotDir, {recursive: true});
const data = {
  schema_version: 1, mode: 'paper', valid: true, fresh: true, healthy: true, state: 'healthy',
  report_age_seconds: 1, max_age_seconds: 20, process_started_utc_s: 900, updated_utc_s: 995,
  account: {equity_inr: 100001.25, cash_inr: 99800, pnl_inr: 1.25, open_orders: 1,
    fees_inr: .25, gross_notional_inr: 201.25, reserved_cash_inr: 100},
  execution_enabled: true,
  sources: ['RELIANCE.NS', 'TCS.NS'].map(symbol => ({symbol, state: 'healthy', source_age_seconds: 59,
    counters: {fetch_attempts: 10, responses: 9, accepted_quotes: 6, duplicate_quotes: 3,
      failures: 1, retries: 1, validation_failures: 0}}))
};
let mode = 'fresh';
const sockets = new Set();
const assets = new Map([['/', ['dashboard.html', 'text/html']], ['/dashboard.css', ['dashboard.css', 'text/css']],
  ['/dashboard.js', ['dashboard.js', 'application/javascript']]]);
const server = createServer(async (request, response) => {
  response.setHeader('Cache-Control', 'no-store');
  response.setHeader('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'");
  if (request.url === '/api/snapshot') {
    if (mode === 'hang') return;
    response.setHeader('Content-Type', 'application/json');
    if (mode === 'malformed') response.end('{');
    else response.end(JSON.stringify(data));
  } else if (assets.has(request.url)) {
    const [file, type] = assets.get(request.url);
    response.setHeader('Content-Type', type + '; charset=utf-8');
    response.end(await readFile(join(root, 'live', file)));
  } else { response.statusCode = 404; response.end(); }
});
server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
await new Promise(resolveListen => server.listen(0, '127.0.0.1', resolveListen));
const url = 'http://127.0.0.1:' + server.address().port + '/';
const chrome = spawn(chromePath, ['--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
  '--no-default-browser-check', '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
  '--user-data-dir=' + profile, 'about:blank'], {stdio: 'ignore', windowsHide: true});
let ws;
const sleep = ms => new Promise(resolveSleep => setTimeout(resolveSleep, ms));
const pending = new Map();
const exceptions = [];
let nextId = 0;
function send(method, params = {}) {
  return new Promise((resolveReply, reject) => {
    const id = ++nextId;
    const timeout = setTimeout(() => { pending.delete(id); reject(new Error('CDP timeout: ' + method)); }, 10000);
    pending.set(id, {resolveReply, reject, timeout});
    ws.send(JSON.stringify({id, method, params}));
  });
}
async function evaluate(expression) {
  const result = await send('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
  assert.equal(result.exceptionDetails, undefined, JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
async function until(expression, description, timeout = 8000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    await sleep(40);
  }
  throw new Error('Browser condition timed out: ' + description);
}
async function refresh() {
  await until("!document.getElementById('refresh').disabled", 'refresh enabled');
  await evaluate("document.getElementById('refresh').click()");
}
async function screenshot(name) {
  if (!screenshotDir) return;
  const result = await send('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
  await writeFile(join(screenshotDir, name + '.png'), Buffer.from(result.data, 'base64'));
}
try {
  let port;
  for (let attempt = 0; attempt < 200; attempt++) {
    try { port = (await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]; break; }
    catch { await sleep(50); }
  }
  assert.ok(port, 'Chrome started its isolated debugging endpoint');
  const pages = await (await fetch('http://127.0.0.1:' + port + '/json/list')).json();
  ws = new WebSocket(pages.find(page => page.type === 'page').webSocketDebuggerUrl);
  await new Promise((resolveOpen, reject) => { ws.addEventListener('open', resolveOpen, {once: true}); ws.addEventListener('error', reject, {once: true}); });
  ws.addEventListener('message', event => {
    const packet = JSON.parse(event.data);
    if (packet.method === 'Runtime.exceptionThrown') exceptions.push(packet.params);
    const item = pending.get(packet.id);
    if (!item) return;
    clearTimeout(item.timeout); pending.delete(packet.id);
    if (packet.error) item.reject(new Error(JSON.stringify(packet.error))); else item.resolveReply(packet.result);
  });
  await send('Runtime.enable');
  await send('Page.enable');
  await send('Emulation.setDeviceMetricsOverride', {width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false});
  await send('Page.navigate', {url});
  await until("document.getElementById('status-title')?.textContent === 'Healthy'", 'healthy rendering');
  assert.match(await evaluate("document.getElementById('equity').textContent"), /1,00,001\.25/);
  assert.equal(await evaluate("document.querySelectorAll('#sources tr').length"), 2);
  await screenshot('dashboard-desktop');
  await send('Emulation.setDeviceMetricsOverride', {width: 390, height: 844, deviceScaleFactor: 1, mobile: false});
  assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true, 'mobile layout fits viewport');
  await screenshot('dashboard-mobile');
  await send('Emulation.setDeviceMetricsOverride', {width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false});
  await evaluate("globalThis.originalDateNow=Date.now;Date.now=()=>originalDateNow()+60000");
  await until("document.getElementById('status-title').textContent === 'Report out of date'", 'wall-clock advance expires cached data');
  assert.equal(await evaluate("document.getElementById('equity').textContent"), '—');
  await evaluate('Date.now=originalDateNow');
  await refresh();
  await until("document.getElementById('status-title').textContent === 'Healthy'", 'fresh data after clock correction');

  data.sources[0].symbol = '<img src=x onerror="globalThis.__xss=1">';
  await refresh();
  await until("document.getElementById('sources').textContent.includes('<img src=x')", 'symbol rendered literally');
  assert.equal(await evaluate("document.querySelectorAll('#sources img').length"), 0);
  assert.equal(await evaluate('Boolean(globalThis.__xss)'), false);
  data.sources[0].symbol = 'RELIANCE.NS';

  data.fresh = false; data.healthy = false; data.report_age_seconds = 30;
  await refresh();
  await until("document.getElementById('status-title').textContent === 'Report out of date'", 'stale state');
  assert.equal(await evaluate("document.getElementById('equity').textContent"), '—');
  assert.ok(await evaluate("Array.from(document.querySelectorAll('#sources tr td:nth-child(4)')).every(cell=>cell.textContent==='—')"));
  await screenshot('dashboard-stale');

  data.fresh = true; data.report_age_seconds = 1; data.state = 'stopped'; data.execution_enabled = false;
  await refresh();
  await until("document.getElementById('status-title').textContent === 'Stopped'", 'clean stop');
  assert.equal(await evaluate("document.getElementById('status-panel').dataset.tone"), 'neutral');
  data.state = 'halted';
  await refresh();
  await until("document.getElementById('status-title').textContent === 'Halted'", 'risk halt');
  assert.equal(await evaluate("document.getElementById('status-panel').dataset.tone"), 'danger');

  data.state = 'healthy'; data.healthy = true; data.execution_enabled = true;
  data.max_age_seconds = 5; data.report_age_seconds = 4;
  await refresh();
  await until("document.getElementById('status-title').textContent === 'Healthy'", 'fresh before expiry');
  mode = 'hang';
  await refresh();
  await until("document.getElementById('status-title').textContent === 'Report out of date'", 'local expiry during pending fetch', 2500);
  assert.equal(await evaluate("document.getElementById('cash').textContent"), '—');
  await until("document.getElementById('status-title').textContent === 'Connection lost'", 'fetch timeout clears state');

  mode = 'fresh'; data.report_age_seconds = 1; data.max_age_seconds = 20;
  await refresh();
  await until("document.getElementById('status-title').textContent === 'Healthy'", 'recovered display');
  mode = 'hang';
  await evaluate("document.dispatchEvent(new Event('visibilitychange'))");
  assert.equal(await evaluate("document.getElementById('equity').textContent"), '—', 'restoration requires new data');
  mode = 'malformed';
  await until("!document.getElementById('refresh').disabled", 'prior request finished');
  await refresh();
  await until("document.getElementById('status-title').textContent === 'Connection lost'", 'malformed payload cleared');
  assert.equal(await evaluate("document.querySelectorAll('#sources tr').length"), 0);
  assert.deepEqual(exceptions, [], 'browser raised no uncaught exceptions');
  console.log('Browser checks passed: desktop/mobile, literal symbols, stale/terminal states, local expiry, timeout, restoration and malformed data.');
} finally {
  ws?.close();
  for (const item of pending.values()) clearTimeout(item.timeout);
  chrome.kill();
  await new Promise(resolveExit => chrome.exitCode !== null ? resolveExit() : chrome.once('exit', resolveExit));
  for (const socket of sockets) socket.destroy();
  await new Promise(resolveClose => server.close(resolveClose));
  // Chrome child processes can briefly retain profile files after parent exit.
  assert.equal(dirname(resolve(temporary)), tempParent);
  assert.ok(basename(temporary).startsWith('paper-dashboard-'));
  await rm(temporary, {recursive: true, force: true, maxRetries: 10, retryDelay: 100});
}
