#include "trading/engine.hpp"
#include "test_support.hpp"

#include <cstddef>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <new>
#ifdef _WIN32
#include <malloc.h>
#endif

namespace {
bool watching = false;
std::size_t allocations = 0;

void count_allocation() noexcept {
    if (watching) ++allocations;
}

void* allocate(std::size_t size) {
    count_allocation();
    if (auto* pointer = std::malloc(size == 0 ? 1 : size)) return pointer;
    throw std::bad_alloc{};
}

void* allocate_aligned(std::size_t size, std::size_t alignment) {
    count_allocation();
#ifdef _WIN32
    if (auto* pointer = _aligned_malloc(size == 0 ? alignment : size, alignment)) return pointer;
#else
    const auto rounded = ((size == 0 ? 1 : size) + alignment - 1) / alignment * alignment;
    if (auto* pointer = std::aligned_alloc(alignment, rounded)) return pointer;
#endif
    throw std::bad_alloc{};
}

void release_aligned(void* pointer) noexcept {
#ifdef _WIN32
    _aligned_free(pointer);
#else
    std::free(pointer);
#endif
}
} // namespace

void* operator new(std::size_t size) { return allocate(size); }
void* operator new[](std::size_t size) { return allocate(size); }
void operator delete(void* pointer) noexcept { std::free(pointer); }
void operator delete[](void* pointer) noexcept { std::free(pointer); }
void operator delete(void* pointer, std::size_t) noexcept { std::free(pointer); }
void operator delete[](void* pointer, std::size_t) noexcept { std::free(pointer); }
void* operator new(std::size_t size, std::align_val_t alignment) { return allocate_aligned(size, static_cast<std::size_t>(alignment)); }
void* operator new[](std::size_t size, std::align_val_t alignment) { return allocate_aligned(size, static_cast<std::size_t>(alignment)); }
void operator delete(void* pointer, std::align_val_t) noexcept { release_aligned(pointer); }
void operator delete[](void* pointer, std::align_val_t) noexcept { release_aligned(pointer); }
void operator delete(void* pointer, std::size_t, std::align_val_t) noexcept { release_aligned(pointer); }
void operator delete[](void* pointer, std::size_t, std::align_val_t) noexcept { release_aligned(pointer); }

int main() {
    using namespace trading;
    EngineConfig c;
    c.strategy_enabled = false;
    c.paper.latency_ns = 0;
    c.paper.slippage_ticks = 0;
    c.paper.fees = {0, 0, 0, 0, 0, 0, 0, 0};
    c.risk.orders_per_second = 1'000;
    c.risk.orders_per_minute = max_session_orders;
    c.risk.orders_per_day = max_session_orders;
    auto engine = std::make_unique<Engine>(c);
    Tick tick{0, 1, 1, 10'000, 10'010, 100, 100, true};
    CHECK(engine->on_tick(tick));

    bool successful = true;
    watching = true;
    for (OrderId id = 1; id <= 200; ++id) {
        const bool buying = id % 2 != 0;
        const Order order{id, 1, buying ? Side::buy : Side::sell, OrderType::limit,
                          TimeInForce::ioc, Product::cnc, buying ? 10'010 : 10'000, 1};
        successful = successful && engine->submit(order) == Reject::none;
        ++tick.time;
        ++tick.sequence;
        successful = successful && engine->on_tick(tick);
        Event event;
        while (engine->pop_event(event)) {}
    }
    engine->heartbeat(tick.time + 1);
    engine->shutdown();
    watching = false;

    CHECK(successful);
    CHECK(engine->position().quantity == 0);
    CHECK(allocations == 0);
    std::cout << "No C++ heap allocations across 200 risk checks, submissions, matching updates, and shutdown\n";
}
