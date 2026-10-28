#pragma once

#include "trading/order_book.hpp"
#include <algorithm>
#include <optional>

namespace trading {

struct StrategyConfig {
    Price momentum_ticks{2};
    Quantity quantity{5};
    Product product{Product::cnc};
    [[nodiscard]] bool valid() const noexcept {
        return momentum_ticks > 0 && momentum_ticks <= 10'000 && quantity > 0 && quantity <= max_quantity &&
               (product == Product::cnc || product == Product::mis || product == Product::nrml);
    }
};

class MomentumStrategy {
public:
    explicit MomentumStrategy(StrategyConfig config = {}) noexcept : config_(config) {}

    [[nodiscard]] std::optional<Order> on_book(const OrderBook& book, const Instrument& instrument,
                                             Quantity position, bool working, OrderId next_id) noexcept {
        const auto mid = book.mid();
        const auto change = initialized_ ? mid - previous_ : 0;
        previous_ = mid;
        initialized_ = true;
        if (!config_.valid() || working || !book.tick().session_open) return std::nullopt;
        const auto threshold = config_.momentum_ticks * instrument.tick_size;
        if (change >= threshold) {
            return Order{next_id, instrument.id, Side::buy, OrderType::limit, TimeInForce::ioc,
                         config_.product, book.tick().ask, config_.quantity};
        }
        if (change <= -threshold && position > 0) {
            Quantity units = std::min(position, config_.quantity);
            units -= units % instrument.lot_size;
            if (units == 0) return std::nullopt;
            return Order{next_id, instrument.id, Side::sell, OrderType::limit, TimeInForce::ioc,
                         config_.product, book.tick().bid, units};
        }
        return std::nullopt;
    }

private:
    StrategyConfig config_;
    Price previous_{};
    bool initialized_{};
};

} // namespace trading
