#pragma once

#include "trading/broker.hpp"
#include "trading/order_book.hpp"
#include <array>

namespace trading {

struct RiskConfig {
    Quantity max_order_quantity{100};
    Quantity max_position{100};
    Money max_total_notional{10'000'000};
    Money max_daily_loss{100'000};
    std::uint32_t max_open_orders{16};
    std::uint32_t orders_per_second{5};
    std::uint32_t orders_per_minute{100};
    std::uint32_t orders_per_day{1'000};
    std::uint32_t price_band_bps{200};
    Time stale_after_ns{2 * second};
    bool allow_short{false};

    [[nodiscard]] bool valid() const noexcept {
        return max_order_quantity > 0 && max_order_quantity <= max_quantity &&
               max_position > 0 && max_position <= max_quantity && max_total_notional > 0 &&
               max_total_notional <= notional(max_price, max_quantity) && max_daily_loss > 0 &&
               max_daily_loss <= max_total_notional && max_open_orders > 0 && max_open_orders <= 64 &&
               orders_per_second > 0 && orders_per_second <= 1'000 && orders_per_minute >= orders_per_second &&
               orders_per_minute <= max_session_orders && orders_per_day >= orders_per_minute &&
               orders_per_day <= max_session_orders && price_band_bps > 0 && price_band_bps <= 10'000 &&
               stale_after_ns > 0 && stale_after_ns <= 60 * second;
    }
};

class RiskManager {
public:
    explicit RiskManager(RiskConfig config = {}) noexcept : config_(config) {}

    // Latches until a new process/session is created. A disconnect never auto-resumes.
    void kill(Reject reason = Reject::killed) noexcept {
        if (kill_reason_ == Reject::none) kill_reason_ = reason;
    }
    void disconnect() noexcept { connected_ = false; kill(Reject::disconnected); }
    [[nodiscard]] bool killed() const noexcept { return kill_reason_ != Reject::none; }
    [[nodiscard]] Reject kill_reason() const noexcept { return kill_reason_; }
    [[nodiscard]] const RiskConfig& config() const noexcept { return config_; }

    [[nodiscard]] Reject health(const OrderBook& book, Time now, Money equity) noexcept {
        if (!config_.valid()) { kill(Reject::invalid); return Reject::invalid; }
        if (killed()) return kill_reason_;
        if (!connected_) { kill(Reject::disconnected); return Reject::disconnected; }
        if (equity <= -config_.max_daily_loss) { kill(Reject::killed); return Reject::killed; }
        if (!book.ready()) return Reject::stale;
        if (now < book.tick().time || now - book.tick().time > config_.stale_after_ns) {
            kill(Reject::stale);
            return Reject::stale;
        }
        if (!book.tick().session_open) return Reject::session_closed;
        return Reject::none;
    }

    template <typename Orders>
    [[nodiscard]] Reject check(const Order& order, Price reservation_price, const Instrument& instrument,
                               const OrderBook& book, const Position& position, const Orders& orders, Time now) noexcept {
        const auto health_result = health(book, now, position.equity(book.mid()));
        if (health_result != Reject::none) return health_result;
        if (!instrument.valid() || order.id == 0 || order.instrument != instrument.id ||
            (order.side != Side::buy && order.side != Side::sell) ||
            (order.type != OrderType::market && order.type != OrderType::limit) ||
            (order.tif != TimeInForce::day && order.tif != TimeInForce::ioc) ||
            (order.product != Product::cnc && order.product != Product::mis && order.product != Product::nrml)) return Reject::invalid;
        if (order.quantity <= 0 || order.quantity > config_.max_order_quantity ||
            order.quantity > instrument.freeze_quantity || order.quantity % instrument.lot_size != 0) return Reject::size;
        if ((order.type == OrderType::limit && (order.price < instrument.lower_band || order.price > instrument.upper_band ||
             order.price % instrument.tick_size != 0)) ||
            reservation_price < instrument.lower_band || reservation_price > instrument.upper_band) return Reject::price_band;
        const Price mid = book.mid();
        if (absolute(reservation_price - mid) * 10'000 > mid * config_.price_band_bps) return Reject::price_band;
        std::int64_t buys{}, sells{};
        Price valuation = reservation_price > mid ? reservation_price : mid;
        std::uint32_t count{};
        for (const auto& state : orders) {
            if (!active(state.status)) continue;
            ++count;
            if (state.order.side == Side::buy) buys += state.remaining;
            else sells += state.remaining;
            if (state.risk_price > valuation) valuation = state.risk_price;
        }
        if (count >= config_.max_open_orders) return Reject::open_orders;
        if (order.side == Side::buy) buys += order.quantity;
        else sells += order.quantity;
        const auto lower = static_cast<std::int64_t>(position.quantity) - sells;
        const auto upper = static_cast<std::int64_t>(position.quantity) + buys;
        if (upper > config_.max_position || lower < -config_.max_position ||
            ((!config_.allow_short || order.product == Product::cnc) && lower < 0)) return Reject::position;
        const Money worst_units = absolute(lower) > absolute(upper) ? absolute(lower) : absolute(upper);
        if (worst_units * valuation > config_.max_total_notional ||
            notional(reservation_price, order.quantity) > config_.max_total_notional) return Reject::notional;
        if (day_count_ >= config_.orders_per_day) return Reject::daily_limit;
        std::uint32_t in_second{}, in_minute{};
        for (std::uint32_t i = day_count_; i > 0; --i) {
            if (now < requests_[i - 1]) return Reject::invalid;
            const Time age = now - requests_[i - 1];
            if (age >= 60 * second) break;
            if (age < second) ++in_second;
            if (age < 60 * second) ++in_minute;
        }
        if (in_second >= config_.orders_per_second || in_minute >= config_.orders_per_minute) return Reject::rate;
        return Reject::none;
    }

    // Charge every request sent to the broker, including deterministic broker rejects.
    void record_request(Time now) noexcept {
        if (day_count_ < requests_.size()) requests_[day_count_++] = now;
        else kill(Reject::daily_limit);
    }

    template <typename Orders>
    [[nodiscard]] Reject check_fill(const OrderState& filling, Price price, Quantity units,
                                    const Position& position, const Orders& orders, Price mid) const noexcept {
        if (absolute(price - mid) * 10'000 > mid * config_.price_band_bps) return Reject::price_band;
        const auto projected = static_cast<std::int64_t>(position.quantity) +
                               (filling.order.side == Side::buy ? units : -units);
        if (projected > config_.max_position || projected < -config_.max_position ||
            ((!config_.allow_short || filling.order.product == Product::cnc) && projected < 0)) return Reject::position;
        std::int64_t buys{}, sells{};
        Price valuation = price > mid ? price : mid;
        for (const auto& state : orders) {
            if (!active(state.status)) continue;
            const Quantity remaining = state.remaining - (state.order.id == filling.order.id ? units : 0);
            if (state.order.side == Side::buy) buys += remaining;
            else sells += remaining;
            if (state.risk_price > valuation) valuation = state.risk_price;
        }
        // Also bound the actual fill turnover, even if its mark differs from execution.
        const auto lower = projected - sells;
        const auto upper = projected + buys;
        const Money worst_units = absolute(lower) > absolute(upper) ? absolute(lower) : absolute(upper);
        if (worst_units * valuation > config_.max_total_notional || notional(price, units) > config_.max_total_notional) return Reject::notional;
        return Reject::none;
    }

private:
    RiskConfig config_;
    std::array<Time, max_session_orders> requests_{};
    std::uint32_t day_count_{};
    bool connected_{true};
    Reject kill_reason_{Reject::none};
};

} // namespace trading
