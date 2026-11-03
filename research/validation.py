"""Independent data, fee and ledger checks for single-instrument paper research."""

import csv
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import re
from urllib.parse import urlparse

HEADER = ('time_ns', 'sequence', 'instrument', 'bid_paise', 'ask_paise',
          'bid_quantity', 'ask_quantity', 'session_open')
AUDIT_HEADER = ('time_ns', 'order_id', 'event', 'side', 'status', 'reason',
                'price_paise', 'quantity', 'fee_paise', 'position', 'equity_paise')
FEE_KEYS = ('brokerage_ppm', 'brokerage_cap_paise', 'stt_buy_ppm', 'stt_sell_ppm',
            'exchange_ppm', 'sebi_ppm', 'stamp_buy_ppm', 'gst_ppm')
DEFAULTS = dict(instrument_id='1', tick_size_paise='5', lot_size='1',
                lower_band_paise='1', upper_band_paise='100000000', product='cnc',
                latency_ns='5000000', slippage_ticks='1', participation_bps='10000',
                brokerage_ppm='300', brokerage_cap_paise='2000', stt_buy_ppm='0',
                stt_sell_ppm='250', exchange_ppm='30', sebi_ppm='1',
                stamp_buy_ppm='30', gst_ppm='180000')


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_config(data: bytes) -> dict:
    result = {}
    for line in data.decode('utf-8').splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        if '=' not in line:
            raise ValueError('Expected key=value in research configuration')
        key, value = (cell.strip() for cell in line.split('=', 1))
        if key in result:
            raise ValueError(f'Duplicate config key: {key}')
        result[key] = value
    if result.get('mode', 'paper') != 'paper':
        raise ValueError('Research only supports paper mode')
    return result


def setting(config: dict, key: str) -> int:
    return int(config.get(key, DEFAULTS[key]))


def validate_data(data: bytes, config: dict) -> tuple[list[dict], dict]:
    reader = csv.reader(io.StringIO(data.decode('utf-8'), newline=''))
    if tuple(next(reader, ())) != HEADER:
        raise ValueError('Unexpected market-data CSV header')
    ticks = []
    previous = None
    max_gap = sequence_gaps = zero_depth = 0
    step = setting(config, 'tick_size_paise')
    lower, upper = setting(config, 'lower_band_paise'), setting(config, 'upper_band_paise')
    if step <= 0 or lower <= 0 or upper > 100_000_000 or lower > upper:
        raise ValueError('Invalid instrument metadata')
    for line, cells in enumerate(reader, 2):
        if not cells:
            continue
        if len(cells) != len(HEADER) or any(not re.fullmatch(r'[0-9]+', c.strip()) for c in cells):
            raise ValueError(f'Data line {line}: expected eight nonnegative integer columns')
        tick = dict(zip(HEADER, (int(c) for c in cells)))
        time, seq = tick['time_ns'], tick['sequence']
        bid, ask = tick['bid_paise'], tick['ask_paise']
        if time > 2**64 - 1 or not 0 < seq <= 2**64 - 1:
            raise ValueError(f'Data line {line}: timestamp/sequence outside native bounds')
        if previous and (time <= previous['time_ns'] or seq <= previous['sequence']):
            raise ValueError(f'Data line {line}: timestamps and sequences must strictly increase')
        if tick['instrument'] != setting(config, 'instrument_id'):
            raise ValueError(f'Data line {line}: instrument differs from configuration')
        if not lower <= bid <= ask <= upper or bid % step or ask % step:
            raise ValueError(f'Data line {line}: crossed, off-tick or out-of-band quote')
        if max(tick['bid_quantity'], tick['ask_quantity']) > 1_000_000 or tick['session_open'] not in (0, 1):
            raise ValueError(f'Data line {line}: invalid depth/session flag')
        if previous:
            max_gap = max(max_gap, time - previous['time_ns'])
            sequence_gaps += max(0, seq - previous['sequence'] - 1)
        zero_depth += int(tick['bid_quantity'] == 0 or tick['ask_quantity'] == 0)
        ticks.append(tick)
        previous = tick
    if not ticks:
        raise ValueError('Market-data CSV contains no ticks')
    return ticks, dict(sha256=digest(data), rows=len(ticks),
                       first_time_ns=ticks[0]['time_ns'], last_time_ns=ticks[-1]['time_ns'],
                       max_gap_ns=max_gap, missing_sequences=sequence_gaps,
                       zero_depth_rows=zero_depth, instrument=setting(config, 'instrument_id'))


def write_ticks(path: Path, ticks: list[dict]) -> None:
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(HEADER)
        writer.writerows([tick[key] for key in HEADER] for tick in ticks)


def fee_provenance(config: dict, profile: Path | None, as_of: str | None,
                   require_verified: bool) -> dict:
    rates = {key: setting(config, key) for key in FEE_KEYS}
    result = dict(status='illustrative', rates=rates,
                  note='No external fee verification; arithmetic fixture only')
    if profile is not None:
        raw = profile.read_bytes()
        record = json.loads(raw)
        if record.get('schema_version') != 1 or record.get('rates') != rates:
            raise ValueError('Fee profile version/rates do not match the effective configuration')
        if record.get('product') != config.get('product', 'cnc') or record.get('currency') != 'INR':
            raise ValueError('Fee profile product/currency does not match')
        if record.get('status') not in ('illustrative', 'verified'):
            raise ValueError('Fee profile status must be illustrative or verified')
        result.update(profile_sha256=digest(raw))
        if record['status'] == 'verified':
            for key in ('broker', 'exchange', 'segment', 'verified_by', 'evidence'):
                if not isinstance(record.get(key), str) or not record[key].strip():
                    raise ValueError(f'Verified fee profile requires {key}')
            try:
                start = date.fromisoformat(record['valid_from'])
                end = date.fromisoformat(record['valid_through'])
                checked = date.fromisoformat(record['verified_on'])
                applicable = date.fromisoformat(as_of)
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError('Verified fee profiles require ISO dates and --as-of') from error
            if not start <= applicable <= end or checked > date.today():
                raise ValueError('Fee profile is outside its effective range or verified in the future')
            sources = record.get('sources')
            if not isinstance(sources, list) or not sources or any(
                    not isinstance(url, str) or urlparse(url).scheme != 'https' or
                    not urlparse(url).netloc for url in sources):
                raise ValueError('Verified fee profiles require HTTPS source links')
            result.update(status='declared_verified', as_of=applicable.isoformat(),
                          broker=record['broker'], exchange=record['exchange'],
                          segment=record['segment'], sources=sources,
                          verified_by=record['verified_by'], verified_on=checked.isoformat(),
                          evidence=record['evidence'],
                          note='Operator attestation; source/contract-note evidence is not independently audited')
    if require_verified and result['status'] != 'declared_verified':
        raise ValueError('Economic validation requires a verified fee profile and --as-of')
    return result


def fee_total(turnover: int, side: str, rates: dict) -> int:
    """Integer reference calculation, cumulative per order, outside the native engine."""
    def charge(value, key):
        return (value * rates[key] + 999_999) // 1_000_000
    brokerage = min(charge(turnover, 'brokerage_ppm'), rates['brokerage_cap_paise'])
    exchange, sebi = charge(turnover, 'exchange_ppm'), charge(turnover, 'sebi_ppm')
    return (brokerage + exchange + sebi + charge(turnover, f'stt_{side}_ppm') +
            (charge(turnover, 'stamp_buy_ppm') if side == 'buy' else 0) +
            charge(brokerage + exchange + sebi, 'gst_ppm'))


def reconcile_audit(report: dict, path: Path, ticks: list[dict], config: dict) -> dict:
    rates = {key: setting(config, key) for key in FEE_KEYS}
    by_time = {tick['time_ns']: tick for tick in ticks}
    orders, depth_used, fills_by_time = {}, {}, {}
    cash = position = fees = fills = accepted = rejected = buys = sells = turnover = slippage_cost = 0
    previous_time = -1
    with path.open(newline='', encoding='utf-8') as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != AUDIT_HEADER:
            raise RuntimeError('Unexpected native audit header')
        for row in reader:
            now, order_id = int(row['time_ns']), int(row['order_id'])
            event, side = row['event'], row['side']
            if now < previous_time or now not in by_time or side not in ('buy', 'sell'):
                raise RuntimeError('Audit event time/side reconciliation failed')
            previous_time = now
            price, units, paid = int(row['price_paise']), int(row['quantity']), int(row['fee_paise'])
            if event == 'accepted':
                if order_id in orders or units <= 0:
                    raise RuntimeError('Audit order ID/quantity reconciliation failed')
                orders[order_id] = dict(side=side, remaining=units, turnover=0, fees=0,
                                        limit=price, time=now, terminal=False)
                accepted += 1
            elif event == 'fill':
                order = orders.get(order_id)
                if (order is None or order['terminal'] or order['side'] != side or
                        not 0 < units <= order['remaining'] or now <= order['time'] or
                        now < order['time'] + setting(config, 'latency_ns')):
                    raise RuntimeError('Audit fill lifecycle/latency reconciliation failed')
                tick = by_time[now]
                touch = tick['ask_paise' if side == 'buy' else 'bid_paise']
                expected_price = touch + (1 if side == 'buy' else -1) * setting(config, 'slippage_ticks') * setting(config, 'tick_size_paise')
                if (not tick['session_open'] or price != expected_price or
                        (price > order['limit'] if side == 'buy' else price < order['limit'])):
                    raise RuntimeError('Audit fill price/session reconciliation failed')
                lot = setting(config, 'lot_size')
                available = tick['ask_quantity' if side == 'buy' else 'bid_quantity'] * setting(config, 'participation_bps') // 10_000
                available -= available % lot
                key = (now, side)
                depth_used[key] = depth_used.get(key, 0) + units
                if units % lot or depth_used[key] > available:
                    raise RuntimeError('Audit shared liquidity reconciliation failed')
                value = price * units
                order['turnover'] += value
                expected_fees = fee_total(order['turnover'], side, rates)
                if paid != expected_fees - order['fees']:
                    raise RuntimeError('Audit fee reconciliation failed')
                order['fees'] = expected_fees
                order['remaining'] -= units
                order['terminal'] = order['remaining'] == 0
                delta_cash = (-value if side == 'buy' else value) - paid
                delta_position = units if side == 'buy' else -units
                cash += delta_cash
                position += delta_position
                fees += paid
                fills += 1
                buys += units if side == 'buy' else 0
                sells += units if side == 'sell' else 0
                turnover += value
                slippage_cost += abs(price - touch) * units
                fills_by_time.setdefault(now, []).append((delta_cash, delta_position))
                mid = tick['bid_paise'] + (tick['ask_paise'] - tick['bid_paise']) // 2
                if int(row['equity_paise']) != cash + position * mid:
                    raise RuntimeError('Audit fill equity reconciliation failed')
            elif event in ('cancelled', 'rejected'):
                order = orders.get(order_id)
                if order is None or order['terminal'] or side != order['side'] or units != order['remaining']:
                    raise RuntimeError('Audit terminal order reconciliation failed')
                order['terminal'] = True
                rejected += event == 'rejected'
            elif event == 'risk':
                rejected += 1
            elif event not in ('signal', 'kill', 'feed'):
                raise RuntimeError('Unknown native audit event')
            if int(row['position']) != position:
                raise RuntimeError('Audit running position reconciliation failed')
    processed = report['ticks']
    if type(processed) is not int or not 0 < processed <= len(ticks) or report['dropped_ticks'] != 0:
        raise RuntimeError('Native tick count reconciliation failed')
    if report['stop_reason'] == 'end_of_replay' and processed != len(ticks):
        raise RuntimeError('Native replay claimed completion before consuming the input')
    if previous_time > ticks[processed - 1]['time_ns']:
        raise RuntimeError('Audit contains events after the final processed tick')
    marked_cash = marked_position = peak = drawdown = 0
    for tick in ticks[:processed]:
        for delta_cash, delta_position in fills_by_time.get(tick['time_ns'], ()):
            marked_cash += delta_cash
            marked_position += delta_position
        mid = tick['bid_paise'] + (tick['ask_paise'] - tick['bid_paise']) // 2
        pnl = marked_cash + marked_position * mid
        peak = max(peak, pnl)
        drawdown = max(drawdown, peak - pnl)
    expected = dict(cash_paise=cash, position=position, fees_paise=fees, pnl_paise=pnl,
                    max_drawdown_paise=drawdown, fills=fills, orders_accepted=accepted,
                    orders_rejected=rejected, open_orders=0)
    if any(report.get(key) != value for key, value in expected.items()) or any(
            not order['terminal'] for order in orders.values()):
        raise RuntimeError('Native summary cash/position/fees/PnL/order reconciliation failed')
    return dict(fill_turnover_paise=turnover, buy_units=buys, sell_units=sells,
                gross_pnl_paise=pnl + fees, adverse_slippage_paise=slippage_cost,
                reconciliation='passed: lifecycle, cash, position, fees, PnL, drawdown, latency, liquidity')


def fresh_output(output: Path, sources: list[Path]) -> None:
    destination = output.resolve()
    if any(source.resolve().is_relative_to(destination) for source in sources):
        raise ValueError('Research outputs must not contain input/config/binary/profile files')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Research output directory must be empty; refusing stale/overwritten artifacts')
    destination.mkdir(parents=True, exist_ok=True)
