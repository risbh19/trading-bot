#include "paper_api.h"
#include "io.hpp"

#include <exception>
#include <limits>
#include <type_traits>

namespace {

struct Context {
    explicit Context(const trading::EngineConfig& settings) noexcept
        : config(settings), engine(settings) {}

    trading::EngineConfig config;
    trading::Engine engine;
    trading::Time last_time{};
};

void copy_text(char* destination, size_t capacity, const char* source) noexcept {
    if (destination == nullptr || capacity == 0) return;
    size_t i{};
    while (i + 1 < capacity && source[i] != '\0') {
        destination[i] = source[i];
        ++i;
    }
    destination[i] = '\0';
}

[[nodiscard]] Context* get(void* context) noexcept {
    return static_cast<Context*>(context);
}

[[nodiscard]] bool valid_time(uint64_t time_ns) noexcept {
    return time_ns <= static_cast<uint64_t>(std::numeric_limits<int64_t>::max());
}

static_assert(std::is_standard_layout_v<PaperSnapshot> && sizeof(PaperSnapshot) == 80);
static_assert(std::is_standard_layout_v<PaperEvent> && sizeof(PaperEvent) == 120);

} // namespace

extern "C" {

int paper_api_version(void) noexcept { return 2; }

void* paper_create(const char* config_path, char* error, size_t error_capacity) noexcept {
    copy_text(error, error_capacity, "");
    if (config_path == nullptr || config_path[0] == '\0') {
        copy_text(error, error_capacity, "A paper configuration path is required");
        return nullptr;
    }
    try {
        const auto config = trading::io::read_config(config_path);
        auto* context = new Context(config);
        if (context->engine.killed()) {
            delete context;
            copy_text(error, error_capacity, "Engine rejected the configuration");
            return nullptr;
        }
        return context;
    } catch (const std::exception& exception) {
        copy_text(error, error_capacity, exception.what());
    } catch (...) {
        copy_text(error, error_capacity, "Unknown error creating the paper engine");
    }
    return nullptr;
}

void paper_destroy(void* context) noexcept {
    if (auto* instance = get(context)) {
        instance->engine.shutdown();
        delete instance;
    }
}

int paper_tick(void* context, uint64_t time_ns, uint64_t sequence,
               int64_t bid, int64_t ask, int32_t bid_qty, int32_t ask_qty,
               int32_t session_open) noexcept {
    auto* instance = get(context);
    if (instance == nullptr) return -1;
    if (!valid_time(time_ns) || (session_open != 0 && session_open != 1)) {
        instance->engine.kill(trading::Reject::invalid);
        return -1;
    }
    const auto& book = instance->engine.book();
    const bool dropped = time_ns < instance->last_time ||
                         (book.ready() && sequence <= book.tick().sequence);
    const trading::Tick tick{time_ns, sequence, instance->config.instrument.id, bid, ask,
                             bid_qty, ask_qty, session_open == 1};
    const bool accepted = instance->engine.on_tick(tick);
    if (accepted) instance->last_time = time_ns;
    return accepted ? 1 : (dropped ? 0 : -1);
}

int paper_heartbeat(void* context, uint64_t time_ns) noexcept {
    auto* instance = get(context);
    if (instance == nullptr) return -1;
    if (!valid_time(time_ns) || time_ns < instance->last_time) {
        instance->engine.kill(trading::Reject::invalid);
        return -1;
    }
    instance->engine.heartbeat(time_ns);
    instance->last_time = time_ns;
    return 1;
}

int paper_cancel_all(void* context, uint64_t time_ns) noexcept {
    auto* instance = get(context);
    if (instance == nullptr) return -1;
    if (!valid_time(time_ns) || time_ns < instance->last_time) {
        instance->engine.kill(trading::Reject::invalid);
        return -1;
    }
    if (!instance->engine.cancel_working_orders(time_ns)) return -1;
    instance->last_time = time_ns;
    return 1;
}

void paper_disconnect(void* context) noexcept {
    if (auto* instance = get(context)) instance->engine.disconnect();
}

void paper_shutdown(void* context) noexcept {
    if (auto* instance = get(context)) instance->engine.shutdown();
}

int paper_snapshot(void* context, PaperSnapshot* output) noexcept {
    auto* instance = get(context);
    if (instance == nullptr || output == nullptr) return -1;
    const auto& engine = instance->engine;
    const auto& position = engine.position();
    const auto& book = engine.book();
    *output = {position.quantity, position.cash, position.fees, engine.equity(),
               book.ready() ? book.tick().bid : 0, book.ready() ? book.tick().ask : 0,
               static_cast<int64_t>(engine.open_orders()), engine.killed() ? 1 : 0,
               engine.audit_failed() ? 1 : 0, static_cast<int64_t>(engine.kill_reason())};
    return 1;
}

int paper_pop_event(void* context, PaperEvent* output) noexcept {
    auto* instance = get(context);
    if (instance == nullptr || output == nullptr) return -1;
    trading::Event event{};
    if (!instance->engine.pop_event(event)) return 0;
    *output = {static_cast<int64_t>(event.time), static_cast<int64_t>(event.order_id),
               static_cast<int64_t>(event.side), static_cast<int64_t>(event.status), event.price,
               event.quantity, event.fee, event.position, event.equity, {}, {}};
    copy_text(output->event, sizeof(output->event), trading::io::name(event.type));
    copy_text(output->reason, sizeof(output->reason), trading::io::name(event.reason));
    return 1;
}

} // extern "C"
