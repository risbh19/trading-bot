#include "trading/engine.hpp"
#include "test_support.hpp"

#include <algorithm>
#include <iostream>
#include <memory>
#include <vector>

namespace {
using namespace trading;

FeeSchedule no_fees() {
    return {0, 0, 0, 0, 0, 0, 0, 0};
}

EngineConfig config() {
    EngineConfig c;
    c.strategy_enabled = false;
    c.paper.latency_ns = 0;
    c.paper.slippage_ticks = 0;
    c.paper.fees = no_fees();
    return c;
}

Tick quote(Time time, std::uint64_t sequence, Quantity liquidity = 100, Price bid = 10'000) {
    return {time, sequence, 1, bid, bid + 10, liquidity, liquidity, true};
}

Order buy(OrderId id, Quantity units = 10, Price price = 10'010, TimeInForce tif = TimeInForce::day) {
    return {id, 1, Side::buy, OrderType::limit, tif, Product::cnc, price, units};
}

std::unique_ptr<Engine> ready(EngineConfig c = config()) {
    auto engine = std::make_unique<Engine>(c);
    CHECK(engine->on_tick(quote(0, 1)));
    return engine;
}

std::vector<Event> drain(Engine& engine) {
    std::vector<Event> events;
    Event event;
    while (engine.pop_event(event)) events.push_back(event);
    return events;
}

const OrderState& state(const Engine& engine, OrderId id) {
    const auto found = std::find_if(engine.orders().begin(), engine.orders().end(),
                                    [id](const auto& order) { return order.order.id == id; });
    CHECK(found != engine.orders().end());
    return *found;
}

void latency_and_ioc() {
    auto c = config();
    c.paper.latency_ns = 100;
    auto engine = ready(c);
    CHECK(engine->submit(buy(1)) == Reject::none);
    CHECK(engine->position().quantity == 0);
    CHECK(engine->on_tick(quote(99, 2)));
    CHECK(engine->position().quantity == 0);
    CHECK(state(*engine, 1).status == Status::pending);
    CHECK(engine->on_tick(quote(100, 3)));
    CHECK(engine->position().quantity == 10);
    CHECK(state(*engine, 1).status == Status::filled);

    engine = ready();
    CHECK(engine->submit(buy(1, 10, 10'010, TimeInForce::ioc)) == Reject::none);
    CHECK(engine->on_tick(quote(1, 2, 3)));
    CHECK(engine->position().quantity == 3);
    CHECK(state(*engine, 1).status == Status::cancelled);
    CHECK(state(*engine, 1).remaining == 7);
    const auto events = drain(*engine);
    CHECK(std::count_if(events.begin(), events.end(), [](const auto& e) { return e.type == EventType::fill; }) == 1);
    CHECK(std::count_if(events.begin(), events.end(), [](const auto& e) { return e.type == EventType::cancelled; }) == 1);
}

void quote_liquidity_and_fifo() {
    auto engine = ready();
    CHECK(engine->submit(buy(1, 5)) == Reject::none);
    CHECK(engine->submit(buy(2, 5)) == Reject::none);
    CHECK(engine->on_tick(quote(1, 2, 6)));
    CHECK(engine->position().quantity == 6);
    CHECK(state(*engine, 1).status == Status::filled);
    CHECK(state(*engine, 2).remaining == 4);
    // New order reuses the filled slot, but cannot overtake the older partial order.
    CHECK(engine->submit(buy(3, 5)) == Reject::none);
    CHECK(engine->on_tick(quote(2, 3, 5)));
    CHECK(engine->position().quantity == 11);
    CHECK(state(*engine, 2).status == Status::filled);
    CHECK(state(*engine, 3).remaining == 4);
    CHECK(!engine->on_tick(quote(2, 3, 100)));
    CHECK(!engine->on_tick(quote(1, 2, 100)));
    CHECK(engine->position().quantity == 11);
}

void fees_and_cash() {
    auto c = config();
    c.paper.fees.brokerage_ppm = 10'000;
    c.paper.fees.brokerage_cap = 50;
    auto engine = ready(c);
    CHECK(engine->submit(buy(1)) == Reject::none);
    CHECK(engine->on_tick(quote(1, 2, 4)));
    CHECK(engine->position().fees == 50);
    CHECK(engine->on_tick(quote(2, 3, 6)));
    CHECK(engine->position().fees == 50); // Per-order cap, not one cap per partial fill.
    CHECK(engine->position().cash == -100'150);
    CHECK(engine->equity() == -100);
    CHECK(charge(999'999, 1) == 1);
    CHECK(charge(1'000'000, 1) == 1);
    CHECK(charge(1'000'001, 1) == 2);

    const FeeSchedule fees{};
    const auto buy_fees = fees_for(fees, 1'000'000, Side::buy);
    const auto sell_fees = fees_for(fees, 1'000'000, Side::sell);
    CHECK(buy_fees.stamp == 30);
    CHECK(sell_fees.stamp == 0);
    CHECK(buy_fees.stt == 0);
    CHECK(sell_fees.stt == 250);
    CHECK(buy_fees.gst == charge(buy_fees.brokerage + buy_fees.exchange + buy_fees.sebi, fees.gst_ppm));
}

void pending_risk_and_validation() {
    auto c = config();
    c.risk.max_position = 10;
    auto engine = ready(c);
    CHECK(engine->submit(buy(1, 6)) == Reject::none);
    CHECK(engine->submit(buy(2, 5)) == Reject::position);
    CHECK(engine->open_orders() == 1);
    CHECK(engine->submit(buy(2, 1)) == Reject::duplicate);
    auto sell = buy(3, 1, 10'000);
    sell.side = Side::sell;
    CHECK(engine->submit(sell) == Reject::position); // A pending buy is not settled inventory.
    CHECK(engine->cancel(1) == Reject::none);
    CHECK(engine->submit(buy(4, 10)) == Reject::none);

    c = config();
    c.risk.max_open_orders = 1;
    engine = ready(c);
    CHECK(engine->submit(buy(1, 1)) == Reject::none);
    CHECK(engine->submit(buy(2, 1)) == Reject::open_orders);

    c = config();
    c.risk.max_total_notional = 150'000;
    engine = ready(c);
    CHECK(engine->submit(buy(1, 10)) == Reject::none);
    CHECK(engine->submit(buy(2, 5)) == Reject::notional);

    engine = ready();
    CHECK(engine->submit(buy(1, 0)) == Reject::size);
    CHECK(engine->submit(buy(2, 101)) == Reject::size);
    CHECK(engine->submit(buy(3, 1, 10'011)) == Reject::price_band);
    CHECK(engine->submit(buy(4, 1, 20'000)) == Reject::price_band);
    auto invalid = buy(5, 1);
    invalid.instrument = 2;
    CHECK(engine->submit(invalid) == Reject::invalid);
    invalid = buy(6, 1);
    invalid.side = static_cast<Side>(7);
    CHECK(engine->submit(invalid) == Reject::invalid);

    c = config();
    c.instrument.lot_size = 5;
    c.instrument.freeze_quantity = 10;
    engine = ready(c);
    CHECK(engine->submit(buy(1, 3)) == Reject::size);
    CHECK(engine->submit(buy(2, 15)) == Reject::size);
    CHECK(engine->submit(buy(3, 10)) == Reject::none);
}

void cancellation_safety() {
    auto engine = ready();
    CHECK(engine->submit(buy(1)) == Reject::none);
    engine->heartbeat(2 * second + 1);
    CHECK(engine->killed());
    CHECK(engine->kill_reason() == Reject::stale);
    CHECK(engine->open_orders() == 0);
    CHECK(engine->on_tick(quote(2 * second + 2, 2)));
    CHECK(engine->position().quantity == 0);
    CHECK(engine->submit(buy(2)) == Reject::stale);

    engine = ready();
    CHECK(engine->submit(buy(1)) == Reject::none);
    // A stale gap must cancel before the replacement quote is matched.
    CHECK(engine->on_tick(quote(3 * second, 2)));
    CHECK(engine->killed());
    CHECK(engine->position().quantity == 0);
    CHECK(engine->open_orders() == 0);

    engine = ready();
    CHECK(engine->submit(buy(1)) == Reject::none);
    engine->disconnect();
    CHECK(engine->open_orders() == 0);
    CHECK(engine->kill_reason() == Reject::disconnected);
    CHECK(engine->on_tick(quote(1, 2)));
    CHECK(engine->position().quantity == 0);

    engine = ready();
    CHECK(engine->submit(buy(1)) == Reject::none);
    auto closed = quote(1, 2);
    closed.session_open = false;
    CHECK(engine->on_tick(closed));
    CHECK(engine->open_orders() == 0);
    CHECK(engine->position().quantity == 0);
    CHECK(engine->submit(buy(2)) == Reject::session_closed);
    CHECK(engine->on_tick(quote(2, 3)));
    CHECK(engine->submit(buy(3)) == Reject::none);
    engine->shutdown();
    CHECK(engine->open_orders() == 0);
    CHECK(engine->killed());

    engine = ready();
    CHECK(engine->submit(buy(1)) == Reject::none);
    auto crossed = quote(1, 2);
    crossed.bid = crossed.ask + 5;
    CHECK(!engine->on_tick(crossed));
    CHECK(engine->kill_reason() == Reject::invalid);
    CHECK(engine->open_orders() == 0);
}

void daily_loss_and_modify() {
    auto c = config();
    c.risk.max_daily_loss = 100;
    auto engine = ready(c);
    CHECK(engine->submit(buy(1, 10)) == Reject::none);
    CHECK(engine->on_tick(quote(1, 2)));
    CHECK(engine->position().quantity == 10);
    CHECK(!engine->killed());
    CHECK(engine->submit(buy(2, 1)) == Reject::none);
    CHECK(engine->on_tick(quote(2, 3, 100, 9'990)));
    CHECK(engine->equity() == -150);
    CHECK(engine->killed());
    CHECK(engine->position().quantity == 10); // The working second order never fills after loss breach.
    CHECK(engine->open_orders() == 0);

    engine = ready();
    CHECK(engine->submit(buy(1)) == Reject::none);
    CHECK(engine->modify(1, buy(2, 101)) == Reject::size);
    CHECK(state(*engine, 1).status == Status::cancelled);
    CHECK(engine->open_orders() == 0);
    CHECK(engine->modify(99, buy(3)) == Reject::unknown_order);
}

void request_limits_and_rejections() {
    auto c = config();
    c.risk.orders_per_second = 1;
    c.risk.orders_per_minute = 2;
    c.risk.orders_per_day = 3;
    auto engine = ready(c);
    CHECK(engine->submit(buy(1, 1)) == Reject::none);
    CHECK(engine->cancel(1) == Reject::none);
    CHECK(engine->submit(buy(2, 1)) == Reject::rate);
    CHECK(engine->on_tick(quote(second, 2)));
    CHECK(engine->submit(buy(3, 1)) == Reject::none);
    CHECK(engine->cancel(3) == Reject::none);
    CHECK(engine->on_tick(quote(2 * second, 3)));
    CHECK(engine->submit(buy(4, 1)) == Reject::rate); // Minute limit still applies.
    // Keep feed fresh while advancing the sliding minute window.
    for (std::uint64_t i = 4; i <= 62; ++i) CHECK(engine->on_tick(quote((i - 1) * second, i)));
    CHECK(engine->submit(buy(5, 1)) == Reject::none);
    CHECK(engine->cancel(5) == Reject::none);
    CHECK(engine->on_tick(quote(62 * second, 63)));
    CHECK(engine->submit(buy(6, 1)) == Reject::daily_limit);

    c = config();
    c.paper.reject_every = 1;
    c.risk.orders_per_second = 1;
    engine = ready(c);
    CHECK(engine->submit(buy(1, 1)) == Reject::broker_reject);
    CHECK(engine->submit(buy(2, 1)) == Reject::rate); // Broker rejects consume request allowance.
    CHECK(engine->open_orders() == 0);
    const auto events = drain(*engine);
    CHECK(events.size() == 2);
}

void market_gap_and_invalid_config() {
    auto c = config();
    c.paper.latency_ns = 10;
    auto engine = ready(c);
    auto market = buy(1, 10);
    market.type = OrderType::market;
    market.price = 0;
    CHECK(engine->submit(market) == Reject::none);
    CHECK(engine->on_tick(quote(10, 2, 100, 10'005)));
    CHECK(state(*engine, 1).status == Status::rejected);
    CHECK(engine->position().quantity == 0);

    c = config();
    c.risk.allow_short = true;
    c.risk.max_total_notional = 10'010;
    c.risk.max_daily_loss = 10'000;
    engine = ready(c);
    auto short_sell = buy(1, 1, 10'000);
    short_sell.side = Side::sell;
    short_sell.product = Product::mis;
    CHECK(engine->submit(short_sell) == Reject::none);
    CHECK(engine->on_tick(quote(1, 2, 100, 20'000)));
    CHECK(engine->position().quantity == 0); // Better execution price cannot bypass gross notional risk.
    CHECK(engine->open_orders() == 0);

    c = config();
    c.risk.orders_per_day = max_session_orders + 1;
    engine = ready(c);
    CHECK(engine->killed());
    CHECK(engine->kill_reason() == Reject::invalid);
    CHECK(engine->submit(buy(1)) == Reject::invalid);

    LiveBroker live;
    CHECK(live.place(buy(1), 0, 10'010) == Reject::live_disabled);
    CHECK(live.cancel(1) == Reject::live_disabled);
}

void reducing_at_notional_cap() {
    auto c = config();
    c.risk.max_total_notional = 100'100;
    auto engine = ready(c);
    CHECK(engine->submit(buy(1, 10)) == Reject::none);
    CHECK(engine->on_tick(quote(1, 2)));
    CHECK(engine->position().quantity == 10);
    auto reducing = buy(2, 10, 10'000);
    reducing.side = Side::sell;
    CHECK(engine->submit(reducing) == Reject::none);
    CHECK(engine->on_tick(quote(2, 3)));
    CHECK(engine->position().quantity == 0);
    const auto events = drain(*engine);
    CHECK(events.back().position == 0);

    c = config();
    c.paper.slippage_ticks = std::numeric_limits<Price>::max();
    engine = ready(c);
    auto market = buy(1);
    market.type = OrderType::market;
    CHECK(engine->submit(market) == Reject::invalid);
}

void capital_exact_and_partial_fees() {
    auto c = config();
    c.initial_capital_paise = 0;
    auto engine = ready(c);
    CHECK(engine->submit(buy(1, 100)) == Reject::none);
    CHECK(engine->on_tick(quote(1, 2)));
    CHECK(engine->position().quantity == 100);
    CHECK(engine->position().cash == -1'001'000); // Zero keeps historical replay unconstrained.

    c = config();
    c.paper.fees.brokerage_ppm = 10'000;
    c.paper.fees.brokerage_cap = 50;
    c.initial_capital_paise = 100'150; // Ten units at 10,010 paise plus one 50-paise order fee.
    engine = ready(c);
    CHECK(engine->submit(buy(1)) == Reject::none);
    CHECK(engine->on_tick(quote(1, 2)));
    CHECK(engine->position().quantity == 10);
    CHECK(engine->position().cash == -c.initial_capital_paise);
    CHECK(engine->submit(buy(2, 1)) == Reject::funds);
    CHECK(!engine->killed());

    --c.initial_capital_paise;
    engine = ready(c);
    CHECK(engine->submit(buy(1)) == Reject::funds); // One paise less than turnover plus fees.
    CHECK(engine->position().cash == 0);
    CHECK(engine->open_orders() == 0);
    CHECK(!engine->killed());

    ++c.initial_capital_paise;
    engine = ready(c);
    CHECK(engine->submit(buy(1)) == Reject::none);
    CHECK(engine->on_tick(quote(1, 2, 4)));
    CHECK(engine->position().quantity == 4);
    CHECK(engine->position().fees == 50);
    CHECK(state(*engine, 1).remaining == 6);
    CHECK(engine->submit(buy(2, 1)) == Reject::funds);
    CHECK(engine->on_tick(quote(2, 3, 6)));
    CHECK(state(*engine, 1).status == Status::filled);
    CHECK(engine->position().quantity == 10);
    CHECK(engine->position().fees == 50); // Remaining reservation must not charge the capped fee twice.
    CHECK(engine->position().cash == -c.initial_capital_paise);
    CHECK(!engine->killed());
}

void capital_reservations_and_cancel() {
    auto c = config();
    c.initial_capital_paise = 200'200;
    auto engine = ready(c);
    CHECK(engine->submit(buy(1)) == Reject::none);
    CHECK(engine->submit(buy(2)) == Reject::none);
    CHECK(engine->position().cash == 0);
    CHECK(engine->submit(buy(3, 1)) == Reject::funds); // Pending orders share the same virtual balance.
    CHECK(engine->cancel(1) == Reject::none);
    CHECK(engine->submit(buy(4)) == Reject::none);
    CHECK(engine->on_tick(quote(1, 2, 15)));
    CHECK(engine->position().quantity == 15);
    CHECK(engine->position().cash == -150'150);
    CHECK(state(*engine, 4).remaining == 5);
    CHECK(engine->submit(buy(5, 1)) == Reject::funds);

    c = config();
    c.paper.fees.brokerage_ppm = 10'000;
    c.paper.fees.brokerage_cap = 50;
    c.initial_capital_paise = 100'150;
    engine = ready(c);
    CHECK(engine->submit(buy(1)) == Reject::none);
    CHECK(engine->on_tick(quote(1, 2, 4)));
    CHECK(engine->position().cash == -40'090);
    CHECK(engine->cancel(1) == Reject::none);
    CHECK(engine->open_orders() == 0);
    // Cancellation releases the unfilled reservation, never the already spent turnover/fees.
    CHECK(engine->submit(buy(2, 6)) == Reject::funds);
    CHECK(engine->submit(buy(3, 5)) == Reject::none);
    CHECK(engine->on_tick(quote(2, 3)));
    CHECK(engine->position().quantity == 9);
    CHECK(engine->position().fees == 100); // The replacement is a separate order with its own cap.
    CHECK(engine->position().cash == -90'190);
    CHECK(!engine->killed());
}

void capital_sale_proceeds_and_loss_basis() {
    auto c = config();
    c.initial_capital_paise = 10'010;
    auto engine = ready(c);
    CHECK(engine->submit(buy(1, 1)) == Reject::none);
    CHECK(engine->on_tick(quote(1, 2)));
    CHECK(engine->submit(buy(2, 1)) == Reject::funds);
    auto sell = buy(3, 1, 10'000);
    sell.side = Side::sell;
    CHECK(engine->submit(sell) == Reject::none);
    CHECK(engine->on_tick(quote(2, 3)));
    CHECK(engine->position().quantity == 0);
    CHECK(engine->position().cash == -10);
    CHECK(engine->on_tick(quote(3, 4, 100, 9'990)));
    CHECK(engine->submit(buy(4, 1, 10'000)) == Reject::none); // Filled sale proceeds fund the next purchase.
    CHECK(engine->on_tick(quote(4, 5, 100, 9'990)));
    CHECK(engine->position().quantity == 1);
    CHECK(engine->position().cash == -10'010);

    c = config();
    c.initial_capital_paise = 10'000'000;
    c.risk.max_daily_loss = 100;
    engine = ready(c);
    CHECK(engine->submit(buy(1)) == Reject::none);
    CHECK(engine->on_tick(quote(1, 2)));
    CHECK(engine->equity() == -50);
    CHECK(!engine->killed());
    CHECK(engine->on_tick(quote(2, 3, 100, 9'990)));
    CHECK(engine->equity() == -150);
    CHECK(c.initial_capital_paise + engine->equity() > 0);
    CHECK(engine->killed()); // Positive account NAV must not conceal the daily trading loss.
    CHECK(engine->kill_reason() == Reject::killed);
}

void audit_backpressure() {
    auto engine = ready();
    CHECK(engine->submit(buy(1)) == Reject::none);
    // An undrained audit queue fails closed, even if the trigger is repeated invalid requests.
    for (OrderId id = 2; id < 2'100; ++id) (void)engine->submit(buy(id, 0));
    CHECK(engine->audit_failed());
    CHECK(engine->killed());
    CHECK(engine->kill_reason() == Reject::capacity);
    CHECK(engine->open_orders() == 0);

    engine = ready();
    CHECK(engine->submit(buy(1)) == Reject::none);
    for (int i = 0; i < 2'048; ++i) CHECK(engine->submit(buy(1)) == Reject::duplicate);
    CHECK(engine->audit_failed());
    CHECK(engine->killed());
    CHECK(engine->open_orders() == 0);

    engine = ready();
    CHECK(engine->submit(buy(1)) == Reject::none);
    CHECK(engine->submit(buy(2)) == Reject::none);
    for (OrderId id = 3; id <= 2'048; ++id) CHECK(engine->submit(buy(id, 0)) == Reject::size);
    CHECK(!engine->audit_failed()); // Queue is exactly full before the cancellation event.
    CHECK(engine->cancel(1) == Reject::none);
    CHECK(engine->audit_failed());
    CHECK(engine->killed());
    CHECK(engine->open_orders() == 0);
}
} // namespace

int main() {
    latency_and_ioc();
    quote_liquidity_and_fifo();
    fees_and_cash();
    pending_risk_and_validation();
    cancellation_safety();
    daily_loss_and_modify();
    request_limits_and_rejections();
    market_gap_and_invalid_config();
    reducing_at_notional_cap();
    capital_exact_and_partial_fees();
    capital_reservations_and_cancel();
    capital_sale_proceeds_and_loss_basis();
    audit_backpressure();
    std::cout << "Execution, risk, and chaos tests passed\n";
}
