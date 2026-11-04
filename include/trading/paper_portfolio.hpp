#pragma once

#include "trading/engine.hpp"
#include <memory>
#include <stdexcept>
#include <vector>

namespace trading {

struct PortfolioConfig {
    Money initial_capital{10'000'000};
    Money max_gross_notional{10'000'000};
    Money max_loss{100'000};
    std::uint32_t max_open_orders{16};
    std::uint32_t orders_per_second{5};
    std::uint32_t orders_per_minute{100};
    std::uint32_t orders_per_session{1'000};

    [[nodiscard]] bool valid() const noexcept {
        const auto ceiling = notional(max_price, max_quantity);
        return initial_capital > 0 && initial_capital <= ceiling &&
               max_gross_notional > 0 && max_gross_notional <= ceiling &&
               max_loss > 0 && max_loss <= initial_capital && max_loss <= max_gross_notional &&
               max_open_orders > 0 && max_open_orders <= 32 * PaperBroker::capacity &&
               orders_per_second > 0 && orders_per_second <= 1'000 &&
               orders_per_minute >= orders_per_second && orders_per_minute <= max_session_orders &&
               orders_per_session >= orders_per_minute && orders_per_session <= max_session_orders;
    }
};

struct PortfolioEvent {
    std::uint32_t instrument{};
    Event event{};
};

// One owner thread, INR cash/CNC, long only. Constituent engines have no capital
// allocation and no automatic submission path; every order passes this account.
class PaperPortfolio {
public:
    static constexpr std::size_t max_instruments = 32;

    PaperPortfolio(PortfolioConfig account, const std::vector<EngineConfig>& configurations)
        : account_(account) {
        if (!account.valid() || configurations.empty() || configurations.size() > max_instruments) {
            throw std::invalid_argument("Invalid paper portfolio account/universe bounds");
        }
        members_.reserve(configurations.size());
        for (const auto& config : configurations) {
            if (config.initial_capital_paise != 0 || config.risk.allow_short ||
                config.strategy.product != Product::cnc || !config.risk.valid() ||
                !config.strategy.valid() || !PaperBroker(config.instrument, config.paper).valid() ||
                config.strategy.quantity % config.instrument.lot_size != 0 ||
                config.strategy.quantity > config.risk.max_order_quantity ||
                find(config.instrument.id) != nullptr) {
                throw std::invalid_argument(
                    "Portfolio needs unique valid instruments, CNC, no shorting and zero constituent capital");
            }
            members_.emplace_back(config, this);
        }
    }

    [[nodiscard]] std::size_t size() const noexcept { return members_.size(); }
    [[nodiscard]] const Engine& engine(std::size_t index) const { return *members_.at(index).engine; }
    [[nodiscard]] const EngineConfig& config(std::size_t index) const { return members_.at(index).config; }
    [[nodiscard]] bool killed() const noexcept { return reason_ != Reject::none; }
    [[nodiscard]] Reject kill_reason() const noexcept { return reason_; }
    [[nodiscard]] bool audit_failed() const noexcept { return audit_failed_; }
    [[nodiscard]] Money initial_capital() const noexcept { return account_.initial_capital; }
    [[nodiscard]] bool ready() const noexcept {
        for (const auto& member : members_) if (!member.engine->book().ready()) return false;
        return true;
    }
    [[nodiscard]] Money cash() const noexcept {
        Money result = account_.initial_capital;
        for (const auto& member : members_) result += member.engine->position().cash;
        return result;
    }
    [[nodiscard]] Money equity() const noexcept { return account_.initial_capital + pnl(); }
    [[nodiscard]] Money pnl() const noexcept {
        Money result{};
        for (const auto& member : members_) result += member.engine->equity();
        return result;
    }
    [[nodiscard]] Money fees() const noexcept {
        Money result{};
        for (const auto& member : members_) result += member.engine->position().fees;
        return result;
    }
    [[nodiscard]] Money gross_notional() const noexcept {
        Money result{};
        for (const auto& member : members_) {
            if (member.engine->book().ready()) {
                result += notional(member.engine->book().mid(), member.engine->position().quantity);
            }
        }
        return result;
    }
    [[nodiscard]] std::uint32_t open_orders() const noexcept {
        std::uint32_t count{};
        for (const auto& member : members_) count += static_cast<std::uint32_t>(member.engine->open_orders());
        return count;
    }
    [[nodiscard]] Money reserved_cash() const noexcept {
        Money result{};
        for (const auto& member : members_) {
            for (const auto& state : member.engine->orders()) {
                if (!active(state.status) || state.order.side != Side::buy) continue;
                const Money value = notional(state.risk_price, state.remaining);
                const Money fees = fees_for(member.config.paper.fees, state.turnover + value, Side::buy).total();
                result += value + std::max<Money>(0, fees - state.fees_paid);
            }
        }
        return result;
    }

    [[nodiscard]] bool on_tick(const Tick& tick) noexcept {
        auto* member = find(tick.instrument);
        if (member == nullptr) { kill(Reject::invalid); return false; }
        const auto& book = member->engine->book();
        if (book.ready() && (tick.sequence <= book.tick().sequence || tick.time < book.tick().time)) return false;
        if (tick.time < now_) { kill(Reject::invalid); return false; }
        auto checked_book = book;
        if (!checked_book.update(tick)) { kill(Reject::invalid); return false; }
        heartbeat(tick.time); // Check every constituent before any new quote can match an order.
        if (!killed()) {
            const auto mark = checked_book.mid();
            if (projected_pnl(tick.instrument, mark) <= -account_.max_loss) kill(Reject::killed);
            else if (projected_gross(nullptr, tick.instrument, mark) > account_.max_gross_notional) {
                kill(Reject::notional);
            }
            if (!tick.session_open) cancel_all();
        }
        const bool accepted = member->engine->on_tick(tick);
        drain(*member);
        assess();
        if (!killed() && member->config.strategy_enabled) {
            const bool blocked = !ready() || !sessions_open() || member->engine->open_orders() != 0;
            auto order = member->strategy.on_book(member->engine->book(), member->config.instrument,
                                                  member->engine->position().quantity, blocked,
                                                  member->last_id + 1);
            if (order) {
                order->price += (order->side == Side::buy ? 1 : -1) *
                                member->config.paper.slippage_ticks * member->config.instrument.tick_size;
                push({tick.instrument, {now_, order->id, EventType::signal, order->side,
                     Status::empty, Reject::none, order->price, order->quantity, 0,
                     member->engine->position().quantity, member->engine->equity()}});
                if (!audit_failed_) (void)submit(*order);
            }
        }
        if (audit_failed_) kill(Reject::capacity);
        return accepted;
    }

    [[nodiscard]] Reject submit(const Order& order) noexcept {
        auto* member = find(order.instrument);
        if (member == nullptr) return reject(order, Reject::invalid, nullptr);
        if (killed()) return reject(order, reason_, member);
        if (order.id == 0 || order.id == std::numeric_limits<OrderId>::max()) {
            return reject(order, Reject::invalid, member);
        }
        if (order.id <= member->last_id) return reject(order, Reject::duplicate, member);
        member->last_id = order.id; // Consume IDs for all rejections, including account-level ones.
        if (order.type != OrderType::limit || order.product != Product::cnc ||
            (order.side != Side::buy && order.side != Side::sell) ||
            (order.tif != TimeInForce::day && order.tif != TimeInForce::ioc) ||
            order.quantity <= 0 || order.quantity > max_quantity ||
            order.price < member->config.instrument.lower_band ||
            order.price > member->config.instrument.upper_band) {
            return reject(order, Reject::invalid, member);
        }
        if (!ready()) return reject(order, Reject::stale, member);
        if (!sessions_open()) return reject(order, Reject::session_closed, member);
        const auto budget = check_budget();
        if (budget != Reject::none) return reject(order, budget, member);
        requests_[request_count_++] = now_; // Account budget includes local and global admission rejects.
        if (open_orders() >= account_.max_open_orders) return reject(order, Reject::open_orders, member);
        if (projected_gross(&order) > account_.max_gross_notional) return reject(order, Reject::notional, member);
        if (order.side == Side::buy) {
            const auto value = notional(order.price, order.quantity);
            const auto needed = value + fees_for(member->config.paper.fees, value, Side::buy).total();
            if (cash() - reserved_cash() < needed) return reject(order, Reject::funds, member);
        }
        const auto result = member->engine->submit(order);
        drain(*member);
        assess();
        return result;
    }

    [[nodiscard]] Reject cancel(std::uint32_t instrument, OrderId order_id) noexcept {
        auto* member = find(instrument);
        if (member == nullptr) return Reject::unknown_order;
        const auto result = member->engine->cancel(order_id);
        drain(*member);
        if (audit_failed_) kill(Reject::capacity);
        return result;
    }
    void cancel_all() noexcept {
        for (auto& member : members_) {
            (void)member.engine->cancel_working_orders(now_);
            drain(member);
        }
        if (audit_failed_) kill(Reject::capacity);
    }
    void heartbeat(Time now) noexcept {
        if (now < now_) { kill(Reject::invalid); return; }
        now_ = now;
        if (killed()) return;
        for (auto& member : members_) {
            if (member.engine->book().ready()) member.engine->heartbeat(now);
            drain(member);
        }
        assess();
    }
    void kill(Reject reason = Reject::killed) noexcept {
        if (!killed()) reason_ = reason == Reject::none ? Reject::killed : reason;
        for (auto& member : members_) {
            member.engine->kill(reason_);
            drain(member); // No recursive queue-failure handler; cancellation must always finish.
        }
    }
    void shutdown() noexcept { kill(); }
    [[nodiscard]] bool pop_event(PortfolioEvent& event) noexcept { return audit_.pop(event); }

private:
    struct Member {
        EngineConfig config;
        std::unique_ptr<Engine> engine;
        MomentumStrategy strategy;
        OrderId last_id{};
        Member(EngineConfig c, PaperPortfolio* owner)
            : config(c), engine(make_engine(c, owner)), strategy(c.strategy) {}
        static std::unique_ptr<Engine> make_engine(EngineConfig config, PaperPortfolio* owner) {
            config.strategy_enabled = false;
            return std::make_unique<Engine>(config, ExecutionGuard{owner, guard_fill, guard_health});
        }
    };
    Member* find(std::uint32_t id) noexcept {
        for (auto& member : members_) if (member.config.instrument.id == id) return &member;
        return nullptr;
    }
    [[nodiscard]] bool sessions_open() const noexcept {
        for (const auto& member : members_) {
            if (!member.engine->book().ready() || !member.engine->book().tick().session_open) return false;
        }
        return true;
    }
    [[nodiscard]] Money projected_pnl(std::uint32_t changed, Price mark) const noexcept {
        Money result{};
        for (const auto& member : members_) {
            result += member.config.instrument.id == changed
                ? member.engine->position().equity(mark) : member.engine->equity();
        }
        return result;
    }
    [[nodiscard]] Money projected_gross(const Order* candidate = nullptr,
                                        std::uint32_t changed = 0, Price mark = 0) const noexcept {
        Money result{};
        for (const auto& member : members_) {
            std::int64_t units = member.engine->position().quantity;
            Price valuation = member.config.instrument.id == changed ? mark :
                (member.engine->book().ready() ? member.engine->book().mid() : 0);
            for (const auto& state : member.engine->orders()) {
                if (!active(state.status) || state.order.side != Side::buy) continue;
                units += state.remaining;
                valuation = std::max(valuation, state.risk_price);
            }
            if (candidate != nullptr && candidate->instrument == member.config.instrument.id &&
                candidate->side == Side::buy) {
                units += candidate->quantity;
                valuation = std::max(valuation, candidate->price);
            }
            result += units * valuation;
        }
        return result;
    }
    [[nodiscard]] Reject check_budget() const noexcept {
        if (request_count_ >= account_.orders_per_session) return Reject::daily_limit;
        std::uint32_t in_second{}, in_minute{};
        for (auto index = request_count_; index > 0; --index) {
            const auto age = now_ - requests_[index - 1];
            if (age >= 60 * second) break;
            if (age < second) ++in_second;
            ++in_minute;
        }
        return in_second >= account_.orders_per_second || in_minute >= account_.orders_per_minute
            ? Reject::rate : Reject::none;
    }
    void assess() noexcept {
        if (audit_failed_) { kill(Reject::capacity); return; }
        if (killed()) return;
        for (const auto& member : members_) {
            if (member.engine->killed()) { kill(member.engine->kill_reason()); return; }
        }
        if (pnl() <= -account_.max_loss) kill(Reject::killed);
        else if (projected_gross() > account_.max_gross_notional) kill(Reject::notional);
        else if (cash() < reserved_cash()) kill(Reject::funds);
    }
    static Reject guard_health(void* context) noexcept {
        const auto& self = *static_cast<PaperPortfolio*>(context);
        if (self.killed()) return self.reason_;
        if (self.pending_halt_ != Reject::none) return self.pending_halt_;
        if (self.pnl() <= -self.account_.max_loss) return Reject::killed;
        if (self.projected_gross() > self.account_.max_gross_notional) return Reject::notional;
        if (self.cash() < self.reserved_cash()) return Reject::funds;
        return Reject::none;
    }
    static Reject guard_fill(void* context, const OrderState& filling,
                             Price price, Quantity units) noexcept {
        auto& self = *static_cast<PaperPortfolio*>(context);
        const auto* member = self.find(filling.order.instrument);
        if (member == nullptr) { self.pending_halt_ = Reject::invalid; return Reject::invalid; }
        const Money value = notional(price, units);
        const Money fee = fees_for(member->config.paper.fees, filling.turnover + value,
                                  filling.order.side).total() - filling.fees_paid;
        const Money projected_cash = self.cash() + (filling.order.side == Side::buy ? -value : value) - fee;
        Money reserved = self.reserved_cash();
        if (filling.order.side == Side::buy) {
            const Money old_value = notional(filling.risk_price, filling.remaining);
            const Money old_fees = fees_for(member->config.paper.fees, filling.turnover + old_value,
                                            Side::buy).total() - filling.fees_paid;
            const Money remaining = notional(filling.risk_price, filling.remaining - units);
            const Money remaining_fees = fees_for(member->config.paper.fees,
                filling.turnover + value + remaining, Side::buy).total() - filling.fees_paid - fee;
            reserved += remaining + std::max<Money>(0, remaining_fees) -
                        old_value - std::max<Money>(0, old_fees);
        }
        if (projected_cash < reserved) {
            self.pending_halt_ = Reject::funds;
            return Reject::funds;
        }
        // Health is re-evaluated after EACH emitted fill, before the matcher can
        // visit the next order. This also catches aggregate fee/slippage losses.
        return guard_health(context);
    }
    void push(const PortfolioEvent& event) noexcept {
        if (!audit_.push(event)) audit_failed_ = true;
    }
    void drain(Member& member) noexcept {
        Event event;
        while (member.engine->pop_event(event)) push({member.config.instrument.id, event});
        audit_failed_ = audit_failed_ || member.engine->audit_failed();
    }
    Reject reject(const Order& order, Reject reason, const Member* member) noexcept {
        push({order.instrument, {now_, order.id, EventType::risk, order.side, Status::rejected,
             reason, order.price, order.quantity, 0,
             member != nullptr ? member->engine->position().quantity : 0,
             member != nullptr ? member->engine->equity() : 0}});
        if (audit_failed_) kill(Reject::capacity);
        return reason;
    }

    // Bounds: <=32 instruments, <=64 orders each, native price/quantity/fee caps
    // and <=10,000 account requests keep all aggregate arithmetic within int64.
    PortfolioConfig account_;
    std::vector<Member> members_;
    SpscQueue<PortfolioEvent, 4096> audit_;
    std::array<Time, max_session_orders> requests_{};
    std::uint32_t request_count_{};
    Time now_{};
    Reject reason_{Reject::none};
    Reject pending_halt_{Reject::none};
    bool audit_failed_{};
};

} // namespace trading
