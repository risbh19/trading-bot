#pragma once

#include "trading/types.hpp"
#include <array>
#include <concepts>

namespace trading {

struct OrderState {
    Order order{};
    Status status{Status::empty};
    Quantity remaining{};
    Time ready_at{};
    Price risk_price{};
    Money turnover{};
    Money fees_paid{};
};

struct Position {
    Quantity quantity{};
    Money cash{}; // Starts at zero: net cash flows from trades, including fees.
    Money fees{};
    [[nodiscard]] Money equity(Price mark) const noexcept { return cash + notional(mark, quantity); }
};

enum class EventType : std::uint8_t { accepted, fill, cancelled, rejected, signal, risk, kill, feed };

struct Event {
    Time time{};
    OrderId order_id{};
    EventType type{};
    Side side{};
    Status status{};
    Reject reason{};
    Price price{};
    Quantity quantity{};
    Money fee{};
    Quantity position{};
    Money equity{};
};

template <typename T>
concept Broker = requires(T broker, Order order, OrderId id, Time now, Price price) {
    { broker.place(order, now, price) } -> std::same_as<Reject>;
    { broker.cancel(id) } -> std::same_as<Reject>;
    { broker.position() } -> std::same_as<const Position&>;
    { broker.orders() };
};

// Live mode deliberately fails closed. No network, credentials, or order routing.
class LiveBroker {
public:
    [[nodiscard]] Reject place(const Order&, Time, Price) noexcept { return Reject::live_disabled; }
    [[nodiscard]] Reject cancel(OrderId) noexcept { return Reject::live_disabled; }
    [[nodiscard]] const Position& position() const noexcept { return position_; }
    [[nodiscard]] const std::array<OrderState, 0>& orders() const noexcept { return orders_; }
private:
    Position position_{};
    std::array<OrderState, 0> orders_{};
};
static_assert(Broker<LiveBroker>);

} // namespace trading
