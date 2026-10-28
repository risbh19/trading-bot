#include "trading/engine.hpp"
#include <algorithm>
#include <chrono>
#include <iostream>
#include <memory>
#include <vector>

namespace {
using Clock = std::chrono::steady_clock;
using Sample = std::chrono::nanoseconds::rep;

void report(const char* name, std::vector<Sample>& samples) {
    std::sort(samples.begin(), samples.end());
    const auto percentile = [&](std::size_t numerator) {
        const auto rank = (samples.size() * numerator + 999) / 1000;
        return samples[std::max<std::size_t>(1, rank) - 1];
    };
    std::cout << name << "," << samples.size() << ',' << percentile(500) << ',' << percentile(900)
              << ',' << percentile(990) << ',' << percentile(999) << '\n';
}
} // namespace

int main() {
    using namespace trading;
    constexpr std::size_t count = 20'000;
    std::vector<Sample> book_samples, pipeline_samples, timer_samples;
    book_samples.reserve(count);
    pipeline_samples.reserve(count);
    timer_samples.reserve(count);
    OrderBook book;
    Tick tick{0, 1, 1, 10'000, 10'010, 100, 100, true};
    std::uint64_t checksum{};
    for (std::size_t i = 0; i < count + 1'000; ++i) {
        ++tick.time;
        ++tick.sequence;
        const auto start = Clock::now();
        const bool accepted = book.update(tick);
        const auto end = Clock::now();
        checksum += accepted ? book.tick().sequence : 0;
        if (i >= 1'000) book_samples.push_back(std::chrono::duration_cast<std::chrono::nanoseconds>(end - start).count());
    }
    EngineConfig config;
    config.strategy.quantity = 1;
    config.paper.latency_ns = 0;
    config.paper.slippage_ticks = 0;
    config.paper.fees = {0, 0, 0, 0, 0, 0, 0, 0};
    config.risk.orders_per_second = 1'000;
    config.risk.orders_per_minute = max_session_orders;
    config.risk.orders_per_day = max_session_orders;
    auto engine = std::make_unique<Engine>(config);
    std::uint64_t accepted_orders{}, fill_events{}, reject_events{};
    for (std::size_t i = 0; i < count + 1'000; ++i) {
        // Reset outside measurement so the benchmark cannot degrade into killed/rate-limit paths.
        if (i % 100 == 0) engine = std::make_unique<Engine>(config);
        tick.time = i * 1'000'000;
        tick.sequence = i + 1;
        tick.bid = 10'000 + static_cast<Price>(i % 4) * 20;
        tick.ask = tick.bid + 10;
        const auto start = Clock::now();
        const bool accepted = engine->on_tick(tick);
        const auto end = Clock::now();
        if (!accepted || engine->killed()) { std::cerr << "Benchmark entered an invalid path\n"; return 1; }
        checksum += engine->open_orders();
        Event event;
        while (engine->pop_event(event)) {
            if (event.type == EventType::accepted) ++accepted_orders;
            if (event.type == EventType::fill) ++fill_events;
            if (event.type == EventType::rejected || event.type == EventType::risk) ++reject_events;
        }
        if (i >= 1'000) pipeline_samples.push_back(std::chrono::duration_cast<std::chrono::nanoseconds>(end - start).count());
    }
    for (std::size_t i = 0; i < count; ++i) {
        const auto start = Clock::now();
        const auto end = Clock::now();
        timer_samples.push_back(std::chrono::duration_cast<std::chrono::nanoseconds>(end - start).count());
    }
    std::cout << "component,samples,p50_ns,p90_ns,p99_ns,p999_ns\n";
    report("timer_baseline", timer_samples);
    report("book_snapshot", book_samples);
    report("tick_strategy_risk_paper_audit", pipeline_samples);
    std::cerr << "checksum=" << checksum << " accepted_orders=" << accepted_orders << " fills=" << fill_events
              << " rejects=" << reject_events << " (includes warmup)\n"
              << "In-memory simulation only. Timer overhead retained; excludes CSV, disk, network and broker/exchange latency.\n";
}
