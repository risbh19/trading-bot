#include "trading/paper_portfolio.hpp"
#include "test_support.hpp"
#include <iostream>

namespace {
using namespace trading;

EngineConfig constituent(std::uint32_t id) {
    EngineConfig c;
    c.instrument.id = id;
    c.strategy_enabled = false;
    c.paper.latency_ns = 0;
    c.paper.slippage_ticks = 0;
    c.paper.fees = {0, 0, 0, 0, 0, 0, 0, 0};
    c.risk.orders_per_second = 100;
    c.risk.orders_per_minute = 1'000;
    return c;
}

PortfolioConfig account() {
    PortfolioConfig c;
    c.initial_capital = 100'000;
    c.max_gross_notional = 1'000'000;
    c.max_loss = 50'000;
    c.orders_per_second = 100;
    c.orders_per_minute = 1'000;
    return c;
}

Tick quote(std::uint32_t id, Time time, std::uint64_t sequence,
           Quantity depth = 100, Price bid = 10'000, bool open = true) {
    return {time, sequence, id, bid, bid + 10, depth, depth, open};
}

Order buy(std::uint32_t instrument, OrderId id, Quantity units = 1, Price price = 10'010) {
    return {id, instrument, Side::buy, OrderType::limit, TimeInForce::day, Product::cnc, price, units};
}

std::unique_ptr<PaperPortfolio> ready(PortfolioConfig limits = account(),
                                    EngineConfig a = constituent(1), EngineConfig b = constituent(2)) {
    auto p = std::make_unique<PaperPortfolio>(limits, std::vector<EngineConfig>{a, b});
    CHECK(p->on_tick(quote(1, 0, 1)));
    CHECK(p->on_tick(quote(2, 0, 1)));
    CHECK(p->ready());
    return p;
}

void shared_cash_and_cancel_release() {
    auto limits = account();
    limits.initial_capital = 15'000;
    limits.max_loss = 10'000;
    auto p = ready(limits);
    CHECK(p->submit(buy(1, 1)) == Reject::none);
    CHECK(p->cash() == 15'000);
    CHECK(p->reserved_cash() == 10'010);
    CHECK(p->submit(buy(2, 1)) == Reject::funds);
    CHECK(p->submit(buy(2, 1)) == Reject::duplicate);
    CHECK(p->cancel(1, 1) == Reject::none);
    CHECK(p->reserved_cash() == 0);
    CHECK(p->submit(buy(2, 2)) == Reject::none);
    CHECK(p->on_tick(quote(2, 1, 2)));
    CHECK(p->cash() == 4'990);
    CHECK(p->engine(0).position().quantity == 0);
    CHECK(p->engine(1).position().quantity == 1);
    CHECK(!p->killed());
}

void cumulative_partial_fill_fee_reservations() {
    auto limits = account();
    limits.initial_capital = 100'150;
    auto a = constituent(1);
    a.paper.fees.brokerage_ppm = 10'000;
    a.paper.fees.brokerage_cap = 50;
    auto p = ready(limits, a);
    CHECK(p->submit(buy(1, 1, 10)) == Reject::none);
    CHECK(p->reserved_cash() == 100'150);
    CHECK(p->on_tick(quote(1, 1, 2, 4)));
    CHECK(p->engine(0).position().quantity == 4);
    CHECK(p->cash() == 60'060);
    CHECK(p->reserved_cash() == 60'060);
    CHECK(p->submit(buy(2, 1)) == Reject::funds);
    CHECK(p->on_tick(quote(1, 2, 3, 6)));
    CHECK(p->cash() == 0);
    CHECK(p->reserved_cash() == 0);
    CHECK(p->fees() == 50);
    CHECK(p->engine(0).position().quantity == 10);
    CHECK(!p->killed());
}

void aggregate_pending_notional_and_open_orders() {
    auto limits = account();
    limits.max_gross_notional = 15'000;
    limits.max_loss = 10'000;
    auto p = ready(limits);
    CHECK(p->submit(buy(1, 1)) == Reject::none);
    CHECK(p->submit(buy(2, 1)) == Reject::notional);
    CHECK(p->open_orders() == 1);
    CHECK(p->cancel(1, 1) == Reject::none);
    CHECK(p->submit(buy(2, 2)) == Reject::none);

    limits = account();
    limits.max_open_orders = 1;
    p = ready(limits);
    CHECK(p->submit(buy(1, 1)) == Reject::none);
    CHECK(p->submit(buy(2, 1)) == Reject::open_orders);
    CHECK(p->cancel(1, 1) == Reject::none);
    CHECK(p->submit(buy(2, 2)) == Reject::none);
}

void aggregate_request_windows_and_consumed_ids() {
    auto limits = account();
    limits.orders_per_second = 1;
    limits.orders_per_minute = 2;
    limits.orders_per_session = 3;
    auto p = ready(limits);
    CHECK(p->submit(buy(1, 1)) == Reject::none);
    CHECK(p->cancel(1, 1) == Reject::none);
    CHECK(p->submit(buy(2, 1)) == Reject::rate);
    CHECK(p->submit(buy(2, 1)) == Reject::duplicate);
    CHECK(p->on_tick(quote(1, second, 2)));
    CHECK(p->on_tick(quote(2, second, 2)));
    CHECK(p->submit(buy(2, 2)) == Reject::none);
    CHECK(p->cancel(2, 2) == Reject::none);
    for (std::uint64_t n = 2; n <= 61; ++n) {
        CHECK(p->on_tick(quote(1, n * second, n + 1)));
        CHECK(p->on_tick(quote(2, n * second, n + 1)));
        if (n == 2) CHECK(p->submit(buy(1, 2)) == Reject::rate);
    }
    CHECK(p->submit(buy(1, 3)) == Reject::none);
    CHECK(p->cancel(1, 3) == Reject::none);
    CHECK(p->on_tick(quote(1, 62 * second, 63)));
    CHECK(p->on_tick(quote(2, 62 * second, 63)));
    CHECK(p->submit(buy(2, 3)) == Reject::daily_limit);
}

void aggregate_marked_loss_cancels_before_replacement_fills() {
    auto limits = account();
    limits.max_loss = 25;
    auto p = ready(limits);
    CHECK(p->submit(buy(1, 1)) == Reject::none);
    CHECK(p->submit(buy(2, 1)) == Reject::none);
    CHECK(p->on_tick(quote(1, 1, 2)));
    CHECK(p->on_tick(quote(2, 2, 2)));
    CHECK(p->pnl() == -10);
    CHECK(p->submit(buy(2, 2)) == Reject::none);
    CHECK(p->on_tick(quote(1, 3, 3, 100, 9'980)));
    CHECK(p->killed());
    CHECK(p->kill_reason() == Reject::killed);
    CHECK(p->open_orders() == 0);
    CHECK(p->engine(0).position().quantity == 1);
    CHECK(p->engine(1).position().quantity == 1);
    CHECK(p->engine(0).killed() && p->engine(1).killed());
    CHECK(p->submit(buy(2, 3)) == Reject::killed);
}

void account_loss_is_checked_between_individual_fills() {
    auto limits = account();
    limits.max_loss = 12;
    auto a = constituent(1);
    a.paper.fees.brokerage_ppm = 1'000'000;
    a.paper.fees.brokerage_cap = 10;
    auto p = ready(limits, a);
    CHECK(p->submit(buy(1, 1)) == Reject::none);
    CHECK(p->submit(buy(1, 2)) == Reject::none);
    CHECK(p->submit(buy(2, 1)) == Reject::none);
    CHECK(p->on_tick(quote(1, 1, 2, 2)));
    CHECK(p->killed());
    CHECK(p->pnl() == -15);
    CHECK(p->engine(0).position().quantity == 1); // Second same-symbol order must not fill.
    CHECK(p->engine(1).position().quantity == 0);
    CHECK(p->open_orders() == 0);
}

void sell_fee_debit_cannot_overdraw_the_shared_account() {
    auto limits = account();
    limits.initial_capital = 20'020;
    limits.max_loss = 20'000;
    auto a = constituent(1);
    a.paper.fees.exchange_ppm = 1'000'000;
    a.paper.fees.stt_sell_ppm = 1'000'000;
    auto p = ready(limits, a);
    CHECK(p->submit(buy(1, 1)) == Reject::none);
    CHECK(p->on_tick(quote(1, 1, 2)));
    CHECK(p->cash() == 0);
    auto sell = buy(1, 2, 1, 10'000);
    sell.side = Side::sell;
    CHECK(p->submit(sell) == Reject::none);
    CHECK(p->on_tick(quote(1, 2, 3)));
    CHECK(p->kill_reason() == Reject::funds);
    CHECK(p->cash() == 0);
    CHECK(p->engine(0).position().quantity == 1);
    CHECK(p->fees() == 10'010);
}

void stale_constituent_stops_fresh_symbol_before_fill() {
    auto p = ready();
    CHECK(p->submit(buy(2, 1, 1, 9'990)) == Reject::none);
    CHECK(p->on_tick(quote(2, second, 2)));
    CHECK(p->on_tick(quote(2, 2 * second, 3)));
    CHECK(!p->killed());
    CHECK(p->on_tick(quote(2, 3 * second, 4, 100, 9'980)));
    CHECK(p->kill_reason() == Reject::stale);
    CHECK(p->engine(1).position().quantity == 0);
    CHECK(p->open_orders() == 0);
    CHECK(p->on_tick(quote(1, 3 * second + 1, 2)));
    CHECK(p->killed()); // A refreshed quote cannot clear the account latch.
}

void warmup_ids_closing_and_regressing_global_clock() {
    auto p = std::make_unique<PaperPortfolio>(account(),
        std::vector<EngineConfig>{constituent(1), constituent(2)});
    CHECK(p->on_tick(quote(1, 0, 1)));
    CHECK(p->submit(buy(1, 1)) == Reject::stale);
    CHECK(p->open_orders() == 0);
    CHECK(p->on_tick(quote(2, 0, 1)));
    CHECK(p->submit(buy(1, 1)) == Reject::duplicate);
    CHECK(p->submit(buy(1, 2)) == Reject::none);
    CHECK(p->submit(buy(2, 2)) == Reject::none); // IDs are keyed by (instrument, order ID).
    CHECK(!p->on_tick(quote(1, 0, 1)));
    CHECK(p->on_tick(quote(1, 1, 2, 100, 10'000, false)));
    CHECK(p->open_orders() == 0);
    CHECK(!p->killed());
    CHECK(p->submit(buy(2, 3)) == Reject::session_closed);
    CHECK(p->on_tick(quote(1, 2, 3)));
    CHECK(p->submit(buy(2, 4)) == Reject::none);
    CHECK(!p->on_tick(quote(2, 1, 2)));
    CHECK(p->kill_reason() == Reject::invalid);
    CHECK(p->open_orders() == 0);
}

void invalid_universe_and_audit_backpressure() {
    for (int scenario = 0; scenario < 4; ++scenario) {
        auto a = constituent(1), b = constituent(2);
        if (scenario == 0) b.instrument.id = 1;
        if (scenario == 1) a.initial_capital_paise = 1;
        if (scenario == 2) a.risk.allow_short = true;
        if (scenario == 3) a.strategy.product = Product::mis;
        bool rejected = false;
        try { (void)ready(account(), a, b); }
        catch (const std::invalid_argument&) { rejected = true; }
        CHECK(rejected);
    }
    auto p = ready();
    CHECK(p->submit(buy(1, 1)) == Reject::none);
    for (OrderId id = 2; id < 5'000 && !p->audit_failed(); ++id) (void)p->submit(buy(2, id, 0));
    CHECK(p->audit_failed());
    CHECK(p->kill_reason() == Reject::capacity);
    CHECK(p->open_orders() == 0);
}
} // namespace

int main() {
    shared_cash_and_cancel_release();
    cumulative_partial_fill_fee_reservations();
    aggregate_pending_notional_and_open_orders();
    aggregate_request_windows_and_consumed_ids();
    aggregate_marked_loss_cancels_before_replacement_fills();
    account_loss_is_checked_between_individual_fills();
    sell_fee_debit_cannot_overdraw_the_shared_account();
    stale_constituent_stops_fresh_symbol_before_fill();
    warmup_ids_closing_and_regressing_global_clock();
    invalid_universe_and_audit_backpressure();
    std::cout << "Shared paper account, fill guards and portfolio risk tests passed\n";
}
