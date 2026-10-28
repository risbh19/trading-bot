#pragma once

#include <cstdint>
#include <limits>
#include <optional>
#include <type_traits>

namespace trading {

using Price = std::int64_t;     // Paise, not rupees.
using Money = std::int64_t;     // Paise.
using Quantity = std::int32_t;  // Units, not lots.
using Time = std::uint64_t;    // Monotonic nanoseconds within a replay session.
using OrderId = std::uint64_t;
constexpr Price max_price = 100'000'000;
constexpr Quantity max_quantity = 1'000'000;
constexpr std::uint32_t max_session_orders = 10'000;
constexpr Time second = 1'000'000'000;

enum class Side : std::uint8_t { buy, sell };
enum class OrderType : std::uint8_t { limit, market };
enum class TimeInForce : std::uint8_t { day, ioc };
enum class Product : std::uint8_t { cnc, mis, nrml };
enum class Status : std::uint8_t { empty, pending, open, partial, filled, cancelled, rejected };
enum class Reject : std::uint8_t {
    none, invalid, killed, disconnected, stale, session_closed, size, position,
    notional, price_band, open_orders, rate, daily_limit, duplicate, capacity,
    unknown_order, live_disabled, broker_reject
};

struct Instrument {
    std::uint32_t id{1};
    Price tick_size{5};
    Quantity lot_size{1};
    Quantity freeze_quantity{1'000};
    Price lower_band{1};
    Price upper_band{max_price};

    [[nodiscard]] bool valid() const noexcept {
        return id != 0 && tick_size > 0 && tick_size <= max_price && lot_size > 0 &&
               freeze_quantity >= lot_size && freeze_quantity <= max_quantity &&
               lower_band > 0 && upper_band <= max_price && lower_band <= upper_band;
    }
};

struct alignas(64) Tick {
    Time time{};
    std::uint64_t sequence{};
    std::uint32_t instrument{1};
    Price bid{};
    Price ask{};
    Quantity bid_quantity{};
    Quantity ask_quantity{};
    bool session_open{true};
};

struct Order {
    OrderId id{}; // Caller-assigned idempotency key, unique for the entire session.
    std::uint32_t instrument{1};
    Side side{Side::buy};
    OrderType type{OrderType::limit};
    TimeInForce tif{TimeInForce::day};
    Product product{Product::cnc};
    Price price{};
    Quantity quantity{};
};

[[nodiscard]] constexpr bool active(Status status) noexcept {
    return status == Status::pending || status == Status::open || status == Status::partial;
}

[[nodiscard]] constexpr Money absolute(Money value) noexcept {
    return value < 0 ? -value : value; // Callers operate within the documented bounded domain.
}

[[nodiscard]] constexpr Money notional(Price price, Quantity quantity) noexcept {
    return price * static_cast<Money>(quantity);
}

[[nodiscard]] constexpr std::optional<Money> checked_add(Money a, Money b) noexcept {
    if ((b > 0 && a > std::numeric_limits<Money>::max() - b) ||
        (b < 0 && a < std::numeric_limits<Money>::min() - b)) {
        return std::nullopt;
    }
    return a + b;
}

static_assert(std::is_trivially_copyable_v<Tick>);
static_assert(sizeof(Tick) == 64);

} // namespace trading
