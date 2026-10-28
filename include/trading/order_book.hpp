#pragma once

#include "trading/types.hpp"

namespace trading {

// Broker top-of-book snapshots; this is deliberately not a depth/L3 order book.
class OrderBook {
public:
    explicit OrderBook(Instrument instrument = {}) noexcept : instrument_(instrument) {}

    [[nodiscard]] bool update(const Tick& next) noexcept {
        if (!instrument_.valid() || next.instrument != instrument_.id || next.sequence == 0 ||
            (ready_ && (next.sequence <= tick_.sequence || next.time < tick_.time)) ||
            next.bid < instrument_.lower_band || next.ask > instrument_.upper_band ||
            next.bid > next.ask || next.bid % instrument_.tick_size != 0 ||
            next.ask % instrument_.tick_size != 0 || next.bid_quantity < 0 ||
            next.ask_quantity < 0 || next.bid_quantity > max_quantity ||
            next.ask_quantity > max_quantity) {
            return false;
        }
        tick_ = next;
        ready_ = true;
        return true;
    }

    [[nodiscard]] bool ready() const noexcept { return ready_; }
    [[nodiscard]] const Tick& tick() const noexcept { return tick_; }
    [[nodiscard]] Price mid() const noexcept { return tick_.bid + (tick_.ask - tick_.bid) / 2; }

private:
    Instrument instrument_;
    Tick tick_{};
    bool ready_{};
};

} // namespace trading
