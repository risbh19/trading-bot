#include "portfolio_api.h"
#include "portfolio_io.hpp"
#include <limits>
#include <type_traits>

namespace {
struct Context {
    explicit Context(const trading::io::PortfolioInput& settings) : engine(settings.account, settings.instruments) {}
    trading::PaperPortfolio engine;
    trading::Time last_time{};
};

void copy_text(char* destination, size_t capacity, const char* source) noexcept {
    if (destination == nullptr || capacity == 0) return;
    size_t index{};
    while (index + 1 < capacity && source[index] != '\0') {
        destination[index] = source[index];
        ++index;
    }
    destination[index] = '\0';
}

Context* get(void* context) noexcept { return static_cast<Context*>(context); }

bool advance(Context* context, uint64_t time_ns) noexcept {
    if (context == nullptr) return false;
    if (time_ns > static_cast<uint64_t>(std::numeric_limits<int64_t>::max()) || time_ns < context->last_time) {
        context->engine.kill(trading::Reject::invalid);
        return false;
    }
    context->engine.heartbeat(time_ns);
    context->last_time = time_ns;
    return true;
}

PaperSnapshot instrument_state(const trading::Engine& engine) noexcept {
    const auto& position = engine.position();
    const auto& book = engine.book();
    return {position.quantity, position.cash, position.fees, engine.equity(),
            book.ready() ? book.tick().bid : 0, book.ready() ? book.tick().ask : 0,
            static_cast<int64_t>(engine.open_orders()), engine.killed() ? 1 : 0,
            engine.audit_failed() ? 1 : 0, static_cast<int64_t>(engine.kill_reason())};
}

static_assert(std::is_standard_layout_v<PortfolioSnapshot> && sizeof(PortfolioSnapshot) == 112);
static_assert(std::is_standard_layout_v<PortfolioInstrumentSnapshot> && sizeof(PortfolioInstrumentSnapshot) == 96);
static_assert(std::is_standard_layout_v<PortfolioAuditEvent> && sizeof(PortfolioAuditEvent) == 128);
} // namespace

extern "C" {
int portfolio_api_version(void) noexcept { return 1; }

void* portfolio_create(const char* path, char* error, size_t capacity) noexcept {
    copy_text(error, capacity, "");
    if (path == nullptr || path[0] == '\0') {
        copy_text(error, capacity, "A paper portfolio configuration path is required");
        return nullptr;
    }
    try {
        return new Context(trading::io::read_portfolio(path));
    } catch (const std::exception& exception) {
        copy_text(error, capacity, exception.what());
    } catch (...) {
        copy_text(error, capacity, "Unknown paper portfolio creation error");
    }
    return nullptr;
}

void portfolio_destroy(void* context) noexcept {
    if (auto* instance = get(context)) {
        instance->engine.shutdown();
        delete instance;
    }
}

int portfolio_tick(void* context, uint64_t now, uint64_t sequence, uint32_t instrument,
                   int64_t bid, int64_t ask, int32_t quantity, int32_t session_open) noexcept {
    auto* instance = get(context);
    if (instance == nullptr) return -1;
    if (now > static_cast<uint64_t>(std::numeric_limits<int64_t>::max()) || sequence == 0 ||
        sequence > static_cast<uint64_t>(std::numeric_limits<int64_t>::max()) ||
        (session_open != 0 && session_open != 1)) {
        instance->engine.kill(trading::Reject::invalid);
        return -1;
    }
    bool dropped = false;
    for (size_t index = 0; index < instance->engine.size(); ++index) {
        if (instance->engine.config(index).instrument.id != instrument) continue;
        const auto& book = instance->engine.engine(index).book();
        dropped = book.ready() && (sequence <= book.tick().sequence || now < book.tick().time);
    }
    const bool accepted = instance->engine.on_tick(
        {now, sequence, instrument, bid, ask, quantity, quantity, session_open == 1});
    instance->last_time = std::max(instance->last_time, now);
    return accepted ? 1 : dropped ? 0 : -1;
}

int portfolio_heartbeat(void* context, uint64_t now) noexcept { return advance(get(context), now) ? 1 : -1; }

int portfolio_suspend(void* context, uint64_t now) noexcept {
    auto* instance = get(context);
    if (!advance(instance, now)) return -1;
    instance->engine.suspend();
    return instance->engine.audit_failed() ? -1 : 1;
}

int portfolio_resume(void* context, uint64_t now) noexcept {
    auto* instance = get(context);
    if (!advance(instance, now)) return -1;
    return instance->engine.resume_execution() ? 1 : 0;
}

void portfolio_disconnect(void* context) noexcept {
    if (auto* instance = get(context)) instance->engine.kill(trading::Reject::disconnected);
}

void portfolio_shutdown(void* context) noexcept {
    if (auto* instance = get(context)) instance->engine.shutdown();
}

int portfolio_snapshot(void* context, PortfolioSnapshot* output) noexcept {
    auto* instance = get(context);
    if (instance == nullptr || output == nullptr) return -1;
    const auto& engine = instance->engine;
    *output = {engine.initial_capital(), engine.cash(), engine.equity(), engine.pnl(), engine.fees(),
               engine.gross_notional(), engine.reserved_cash(), engine.open_orders(), engine.killed() ? 1 : 0,
               engine.audit_failed() ? 1 : 0, static_cast<int64_t>(engine.kill_reason()),
               static_cast<int64_t>(engine.size()), engine.ready() ? 1 : 0, engine.execution_enabled() ? 1 : 0};
    return 1;
}

int portfolio_instrument_snapshot(void* context, size_t index, PortfolioInstrumentSnapshot* output) noexcept {
    auto* instance = get(context);
    if (instance == nullptr || output == nullptr || index >= instance->engine.size()) return -1;
    const auto& engine = instance->engine.engine(index);
    *output = {instance->engine.config(index).instrument.id, engine.book().ready() ? 1 : 0,
               instrument_state(engine)};
    return 1;
}

int portfolio_pop_event(void* context, PortfolioAuditEvent* output) noexcept {
    auto* instance = get(context);
    if (instance == nullptr || output == nullptr) return -1;
    trading::PortfolioEvent tagged;
    if (!instance->engine.pop_event(tagged)) return 0;
    const auto& event = tagged.event;
    *output = {tagged.instrument, {static_cast<int64_t>(event.time), static_cast<int64_t>(event.order_id),
        static_cast<int64_t>(event.side), static_cast<int64_t>(event.status), event.price, event.quantity,
        event.fee, event.position, event.equity, {}, {}}};
    copy_text(output->event.event, sizeof(output->event.event), trading::io::name(event.type));
    copy_text(output->event.reason, sizeof(output->event.reason), trading::io::name(event.reason));
    return 1;
}
}
