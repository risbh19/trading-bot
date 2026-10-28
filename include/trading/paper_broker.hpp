#pragma once

#include "trading/broker.hpp"
#include "trading/fees.hpp"
#include <algorithm>
#include <array>
#include <limits>

namespace trading {

struct PaperConfig {
    Time latency_ns{5'000'000};
    Price slippage_ticks{1};
    std::uint32_t reject_every{0}; // Deterministic fault injection, disabled by default.
    FeeSchedule fees{};
};

class PaperBroker {
public:
    static constexpr std::size_t capacity = 64;
    explicit PaperBroker(Instrument instrument = {}, PaperConfig config = {}) noexcept
        : instrument_(instrument), config_(config) {}

    [[nodiscard]] bool valid() const noexcept {
        return instrument_.valid() && config_.fees.valid() && config_.latency_ns <= 60 * second &&
               config_.slippage_ticks >= 0 && config_.slippage_ticks <= 1'000;
    }

    [[nodiscard]] Reject place(const Order& order, Time now, Price risk_price) noexcept {
        if (!valid() || order.id == 0 || order.instrument != instrument_.id ||
            order.quantity <= 0 || order.quantity > max_quantity ||
            order.quantity % instrument_.lot_size != 0 ||
            risk_price <= 0 || risk_price > max_price || now > std::numeric_limits<Time>::max() - config_.latency_ns) {
            return Reject::invalid;
        }
        if (order.id <= last_id_) return Reject::duplicate;
        if (seen_count_ == max_session_orders) return Reject::daily_limit;
        // Remember even rejected IDs: retries can never create a second order.
        last_id_ = order.id;
        ++seen_count_;
        if (config_.reject_every != 0 && seen_count_ % config_.reject_every == 0) return Reject::broker_reject;
        for (auto& state : orders_) {
            if (!active(state.status)) {
                state = {order, Status::pending, order.quantity, now + config_.latency_ns, risk_price, 0, 0};
                return Reject::none;
            }
        }
        return Reject::capacity;
    }

    [[nodiscard]] Reject cancel(OrderId id) noexcept {
        for (auto& state : orders_) {
            if (state.order.id == id && active(state.status)) {
                state.status = Status::cancelled;
                return Reject::none;
            }
        }
        return Reject::unknown_order;
    }

    template <typename Emit>
    void cancel_all(Time now, Emit&& emit) noexcept {
        for (auto& state : orders_) {
            if (!active(state.status)) continue;
            state.status = Status::cancelled;
            (void)emit(event(now, state, EventType::cancelled));
        }
    }

    template <typename Emit>
    void on_tick(const Tick& tick, Emit&& emit) noexcept {
        on_tick(tick, emit, [](const OrderState&, Price, Quantity) noexcept { return Reject::none; });
    }

    template <typename Emit, typename CanFill>
    void on_tick(const Tick& tick, Emit&& emit, CanFill&& can_fill) noexcept {
        Quantity bid_liquidity = tick.bid_quantity;
        Quantity ask_liquidity = tick.ask_quantity;
        // FIFO by monotonically increasing accepted ID. Slot reuse must not jump the queue.
        OrderId after{};
        for (std::size_t n = 0; n < capacity; ++n) {
            OrderState* next = nullptr;
            for (auto& candidate : orders_) {
                if (active(candidate.status) && candidate.order.id > after &&
                    (next == nullptr || candidate.order.id < next->order.id)) next = &candidate;
            }
            if (next == nullptr) break;
            auto& state = *next;
            after = state.order.id;
            if (tick.time < state.ready_at) continue;
            state.status = state.remaining == state.order.quantity ? Status::open : Status::partial;
            const bool buy = state.order.side == Side::buy;
            const Price touch = buy ? tick.ask : tick.bid;
            // Deterministic adverse slippage, tick aligned. Never violate a limit price.
            const Price slipped = touch + (buy ? 1 : -1) * config_.slippage_ticks * instrument_.tick_size;
            const bool crosses = state.order.type == OrderType::market ||
                (buy ? state.order.price >= slipped : state.order.price <= slipped);
            auto& liquidity = buy ? ask_liquidity : bid_liquidity;
            if (slipped < instrument_.lower_band || slipped > instrument_.upper_band) {
                state.status = Status::rejected;
                if (!emit(event(tick.time, state, EventType::rejected, Reject::price_band))) return;
                continue;
            }
            if (state.order.type == OrderType::market &&
                (buy ? slipped > state.risk_price : slipped < state.risk_price)) {
                state.status = Status::rejected;
                if (!emit(event(tick.time, state, EventType::rejected, Reject::price_band))) return;
                continue;
            }
            if (crosses && liquidity > 0) {
                const Quantity units = std::min(state.remaining, static_cast<Quantity>(
                    liquidity - liquidity % instrument_.lot_size));
                if (units > 0) {
                    const auto approval = can_fill(state, slipped, units);
                    if (approval != Reject::none) {
                        state.status = Status::rejected;
                        if (!emit(event(tick.time, state, EventType::rejected, approval))) return;
                        continue;
                    }
                    const Money value = notional(slipped, units);
                    const Money cumulative = state.turnover + value;
                    const Money cumulative_fees = fees_for(config_.fees, cumulative, state.order.side).total();
                    const Money incremental_fee = cumulative_fees - state.fees_paid;
                    const auto cash = checked_add(position_.cash, (buy ? -value : value) - incremental_fee);
                    if (!cash) {
                        state.status = Status::rejected;
                        (void)emit(event(tick.time, state, EventType::rejected, Reject::notional));
                        return;
                    }
                    position_.cash = *cash;
                    position_.quantity += buy ? units : -units;
                    position_.fees += incremental_fee;
                    state.turnover = cumulative;
                    state.fees_paid = cumulative_fees;
                    state.remaining -= units;
                    liquidity -= units;
                    state.status = state.remaining == 0 ? Status::filled : Status::partial;
                    auto fill = event(tick.time, state, EventType::fill);
                    fill.price = slipped;
                    fill.quantity = units;
                    fill.fee = incremental_fee;
                    fill.position = position_.quantity;
                    if (!emit(fill)) return;
                }
            }
            // IOC and market remainder cannot rest on the book.
            if (active(state.status) && (state.order.tif == TimeInForce::ioc || state.order.type == OrderType::market)) {
                state.status = Status::cancelled;
                if (!emit(event(tick.time, state, EventType::cancelled))) return;
            }
        }
    }

    [[nodiscard]] const Position& position() const noexcept { return position_; }
    [[nodiscard]] const std::array<OrderState, capacity>& orders() const noexcept { return orders_; }
    [[nodiscard]] const PaperConfig& config() const noexcept { return config_; }

    [[nodiscard]] static Event event(Time now, const OrderState& state, EventType type, Reject reason = Reject::none) noexcept {
        return {now, state.order.id, type, state.order.side, state.status, reason,
                state.order.price, state.remaining, 0, 0, 0};
    }

private:
    Instrument instrument_;
    PaperConfig config_;
    std::array<OrderState, capacity> orders_{};
    OrderId last_id_{};
    std::size_t seen_count_{};
    Position position_{};
};
static_assert(Broker<PaperBroker>);

} // namespace trading
