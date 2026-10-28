#pragma once

#include "trading/paper_broker.hpp"
#include "trading/risk.hpp"
#include "trading/spsc_queue.hpp"
#include "trading/strategy.hpp"

namespace trading {

struct EngineConfig {
    Instrument instrument{};
    PaperConfig paper{};
    RiskConfig risk{};
    StrategyConfig strategy{};
    bool strategy_enabled{true};
    Money initial_capital_paise{0}; // Zero preserves unconstrained historical replay.
};

class Engine {
public:
    explicit Engine(EngineConfig config = {}) noexcept
        : config_(config), book_(config.instrument), broker_(config.instrument, config.paper),
          risk_(config.risk), strategy_(config.strategy) {
        if (!broker_.valid() || !config.risk.valid() || !config.strategy.valid() ||
            config.initial_capital_paise < 0 || config.initial_capital_paise > notional(max_price, max_quantity)) {
            risk_.kill(Reject::invalid);
        }
    }

    [[nodiscard]] bool on_tick(const Tick& tick) noexcept {
        // Reject duplicates/regressions before matching, so they cannot replenish liquidity.
        if (tick.time < now_ || (book_.ready() && tick.sequence <= book_.tick().sequence)) return false;
        if (book_.ready()) heartbeat(tick.time); // Stale gap cancels before new quotes can fill old orders.
        if (!book_.update(tick)) {
            risk_.kill(Reject::invalid);
            shutdown_orders();
            return false;
        }
        now_ = tick.time;
        const auto health = assess_health();
        if (health != Reject::none) { shutdown_orders(); return true; }
        broker_.on_tick(tick, [this](Event event) noexcept {
            event.equity = equity();
            if (!emit(event)) return false;
            return assess_health() == Reject::none;
        }, [this](const OrderState& state, Price price, Quantity units) noexcept {
            const auto result = risk_.check_fill(state, price, units, position(), broker_.orders(), book_.mid());
            return result == Reject::none ? check_fill_cash(state, price, units) : result;
        });
        if (risk_.killed()) { shutdown_orders(); return true; }
        if (config_.strategy_enabled) {
            auto order = strategy_.on_book(book_, config_.instrument, broker_.position().quantity,
                                           open_orders() != 0, next_id_);
            if (order) {
                // Make the sample IOC marketable after configured adverse slippage.
                order->price += (order->side == Side::buy ? 1 : -1) * config_.paper.slippage_ticks * config_.instrument.tick_size;
                (void)emit({now_, order->id, EventType::signal, order->side, Status::empty, Reject::none,
                            order->price, order->quantity, 0, broker_.position().quantity, equity()});
                (void)submit(*order);
            }
        }
        return true;
    }

    [[nodiscard]] Reject submit(const Order& order) noexcept {
        if (risk_.killed()) {
            record_reject(order, risk_.kill_reason());
            shutdown_orders();
            return risk_.kill_reason();
        }
        if (order.id == 0 || order.id < next_id_ || order.id == std::numeric_limits<OrderId>::max()) {
            record_reject(order, Reject::duplicate);
            if (risk_.killed()) shutdown_orders();
            return Reject::duplicate;
        }
        // Consume IDs even for pre-trade rejections. Retries must use reconciliation/new IDs.
        next_id_ = order.id + 1;
        const Price reservation = order.type == OrderType::limit ? order.price :
            (order.side == Side::buy ? book_.tick().ask : book_.tick().bid) +
            (order.side == Side::buy ? 1 : -1) * config_.paper.slippage_ticks * config_.instrument.tick_size;
        auto result = risk_.check(order, reservation, config_.instrument, book_, broker_.position(), broker_.orders(), now_);
        if (result == Reject::none && config_.initial_capital_paise != 0 && order.side == Side::buy) {
            const auto value = notional(reservation, order.quantity);
            const auto required = buy_reservations() + value + fees_for(config_.paper.fees, value, Side::buy).total();
            const auto balance = checked_add(config_.initial_capital_paise, position().cash);
            if (!balance || *balance < required) result = Reject::funds;
        }
        if (result == Reject::none) {
            risk_.record_request(now_);
            result = broker_.place(order, now_, reservation);
        }
        if (result != Reject::none) {
            record_reject(order, result);
            if (risk_.killed()) shutdown_orders();
        } else {
            (void)emit({now_, order.id, EventType::accepted, order.side, Status::pending, Reject::none,
                        order.price, order.quantity, 0, broker_.position().quantity, equity()});
        }
        if (risk_.killed()) shutdown_orders();
        return result;
    }

    [[nodiscard]] Reject cancel(OrderId id) noexcept {
        for (const auto& state : broker_.orders()) {
            if (state.order.id == id && active(state.status)) {
                auto event = PaperBroker::event(now_, state, EventType::cancelled);
                event.status = Status::cancelled;
                const auto result = broker_.cancel(id);
                (void)emit(event);
                if (risk_.killed()) shutdown_orders();
                return result;
            }
        }
        return Reject::unknown_order;
    }

    // Cancel-first replacement: replacement failure leaves the old order cancelled.
    [[nodiscard]] Reject modify(OrderId old_id, const Order& replacement) noexcept {
        const auto result = cancel(old_id);
        return result == Reject::none ? submit(replacement) : result;
    }

    void heartbeat(Time now) noexcept {
        if (now < now_) { kill(Reject::invalid); return; }
        now_ = now;
        if (assess_health() != Reject::none) shutdown_orders();
    }
    void disconnect() noexcept { risk_.disconnect(); shutdown_orders(); }
    void kill(Reject reason = Reject::killed) noexcept { risk_.kill(reason); shutdown_orders(); }
    void shutdown() noexcept { kill(); }

    [[nodiscard]] bool pop_event(Event& event) noexcept { return audit_.pop(event); }
    [[nodiscard]] const Position& position() const noexcept { return broker_.position(); }
    [[nodiscard]] const auto& orders() const noexcept { return broker_.orders(); }
    [[nodiscard]] const OrderBook& book() const noexcept { return book_; }
    [[nodiscard]] bool killed() const noexcept { return risk_.killed(); }
    [[nodiscard]] Reject kill_reason() const noexcept { return risk_.kill_reason(); }
    [[nodiscard]] bool audit_failed() const noexcept { return audit_failed_; }
    [[nodiscard]] Money equity() const noexcept { return book_.ready() ? position().equity(book_.mid()) : position().cash; }
    [[nodiscard]] std::size_t open_orders() const noexcept {
        std::size_t count{};
        for (const auto& state : broker_.orders()) if (active(state.status)) ++count;
        return count;
    }

private:
    [[nodiscard]] Money reserve_buy(const OrderState& state, Quantity remaining,
                                     Money turnover, Money paid) const noexcept {
        const Money value = notional(state.risk_price, remaining);
        const Money fees = fees_for(config_.paper.fees, turnover + value, Side::buy).total() - paid;
        return value + (fees > 0 ? fees : 0);
    }
    [[nodiscard]] Money buy_reservations() const noexcept {
        Money reserved{};
        for (const auto& state : broker_.orders()) {
            if (active(state.status) && state.order.side == Side::buy) {
                reserved += reserve_buy(state, state.remaining, state.turnover, state.fees_paid);
            }
        }
        return reserved;
    }
    [[nodiscard]] Reject check_fill_cash(const OrderState& filling, Price price, Quantity units) const noexcept {
        if (config_.initial_capital_paise == 0) return Reject::none;
        const Money value = notional(price, units);
        const Money new_turnover = filling.turnover + value;
        const Money new_fees = fees_for(config_.paper.fees, new_turnover, filling.order.side).total();
        const Money delta = new_fees - filling.fees_paid;
        const auto balance = checked_add(config_.initial_capital_paise, position().cash);
        if (!balance) return Reject::funds;
        const auto projected = checked_add(*balance, (filling.order.side == Side::buy ? -value : value) - delta);
        if (!projected) return Reject::funds;
        Money reserved{};
        for (const auto& state : broker_.orders()) {
            if (!active(state.status) || state.order.side != Side::buy) continue;
            if (state.order.id == filling.order.id) {
                reserved += reserve_buy(state, state.remaining - units, new_turnover, new_fees);
            } else reserved += reserve_buy(state, state.remaining, state.turnover, state.fees_paid);
        }
        return *projected >= reserved ? Reject::none : Reject::funds;
    }
    [[nodiscard]] Reject assess_health() noexcept {
        if (book_.ready() && absolute(notional(book_.mid(), position().quantity)) > config_.risk.max_total_notional) {
            risk_.kill(Reject::notional);
        }
        return risk_.health(book_, now_, equity());
    }
    [[nodiscard]] bool emit(Event event) noexcept {
        event.position = position().quantity;
        event.equity = equity();
        if (audit_.push(event)) return true;
        audit_failed_ = true;
        risk_.kill(Reject::capacity);
        return false;
    }
    void record_reject(const Order& order, Reject reason) noexcept {
        (void)emit({now_, order.id, EventType::risk, order.side, Status::rejected, reason,
                    order.price, order.quantity, 0, position().quantity, equity()});
    }
    void shutdown_orders() noexcept {
        if (risk_.killed() && !kill_logged_) {
            kill_logged_ = true;
            (void)emit({now_, 0, EventType::kill, Side::buy, Status::empty, risk_.kill_reason(), 0, 0, 0,
                        position().quantity, equity()});
        }
        broker_.cancel_all(now_, [this](Event event) noexcept { return emit(event); });
    }

    EngineConfig config_;
    OrderBook book_;
    PaperBroker broker_;
    RiskManager risk_;
    MomentumStrategy strategy_;
    SpscQueue<Event, 2048> audit_;
    Time now_{};
    OrderId next_id_{1};
    bool audit_failed_{};
    bool kill_logged_{};
};

} // namespace trading
