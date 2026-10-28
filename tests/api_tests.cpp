#include "../app/paper_api.h"
#include "test_support.hpp"

#include <algorithm>
#include <array>
#include <cstring>
#include <fstream>
#include <iostream>
#include <limits>
#include <memory>
#include <string>
#include <string_view>

namespace {

using Handle = std::unique_ptr<void, decltype(&paper_destroy)>;
constexpr uint64_t first_time = 1'000'000'000;

struct Quote {
    int64_t price{};
    int32_t quantity{};
};

// Derive one valid locked quote without depending on internal C++ engine headers.
Quote quote_for(const char* path) {
    std::ifstream config(path);
    CHECK(config.good());
    int64_t tick = 5;
    int64_t lower = 1;
    int64_t upper = 100'000'000;
    int32_t lot = 1;
    std::string line;
    while (std::getline(config, line)) {
        line = line.substr(0, line.find('#'));
        const auto equal = line.find('=');
        if (equal == std::string::npos) continue;
        auto key = line.substr(0, equal);
        const auto start = key.find_first_not_of(" \t\r");
        if (start == std::string::npos) continue;
        key = key.substr(start, key.find_last_not_of(" \t\r") - start + 1);
        const auto value = line.substr(equal + 1);
        if (key == "tick_size_paise") tick = std::stoll(value);
        else if (key == "lower_band_paise") lower = std::stoll(value);
        else if (key == "upper_band_paise") upper = std::stoll(value);
        else if (key == "lot_size") lot = static_cast<int32_t>(std::stoll(value));
    }
    CHECK(tick > 0 && lower > 0 && upper >= lower && lot > 0);
    const int64_t first_tick = ((lower + tick - 1) / tick) * tick;
    CHECK(first_tick <= upper);
    return {first_tick, lot};
}

Handle create(const char* path) {
    std::array<char, 512> error{};
    Handle handle(paper_create(path, error.data(), error.size()), paper_destroy);
    if (!handle) std::cerr << "paper_create: " << error.data() << '\n';
    CHECK(handle != nullptr);
    CHECK(error[0] == '\0');
    return handle;
}

int tick(void* handle, const Quote& quote, uint64_t time = first_time,
         uint64_t sequence = 1, int32_t session_open = 1) {
    return paper_tick(handle, time, sequence, quote.price, quote.price,
                      quote.quantity, quote.quantity, session_open);
}

std::size_t drain(void* handle) {
    constexpr std::array<std::string_view, 8> names{
        "accepted", "fill", "cancelled", "rejected", "signal", "risk", "kill", "feed"};
    std::size_t count{};
    PaperEvent event{};
    int result{};
    while ((result = paper_pop_event(handle, &event)) == 1) {
        CHECK(std::memchr(event.event, '\0', sizeof(event.event)) != nullptr);
        CHECK(std::memchr(event.reason, '\0', sizeof(event.reason)) != nullptr);
        CHECK(event.reason[0] != '\0');
        CHECK(std::find(names.begin(), names.end(), std::string_view(event.event)) != names.end());
        CHECK(event.side == 0 || event.side == 1);
        CHECK(event.time_ns >= 0);
        ++count;
        CHECK(count <= 2'048);
    }
    CHECK(result == 0);
    CHECK(paper_pop_event(handle, &event) == 0);
    return count;
}

void null_guards() {
    PaperSnapshot snapshot{};
    PaperEvent event{};
    CHECK(paper_snapshot(nullptr, &snapshot) == -1);
    CHECK(paper_snapshot(nullptr, nullptr) == -1);
    CHECK(paper_pop_event(nullptr, &event) == -1);
    CHECK(paper_pop_event(nullptr, nullptr) == -1);
    CHECK(paper_tick(nullptr, 0, 1, 10'000, 10'000, 1, 1, 1) == -1);
    CHECK(paper_heartbeat(nullptr, 0) == -1);
    paper_disconnect(nullptr);
    paper_shutdown(nullptr);
    paper_destroy(nullptr);

    std::array<char, 64> error{};
    CHECK(paper_create(nullptr, error.data(), error.size()) == nullptr);
    CHECK(error[0] != '\0');
    CHECK(paper_create("", error.data(), error.size()) == nullptr);
    CHECK(error[0] != '\0');
}

void bounded_errors(const char* path) {
    const std::string missing = std::string(path) + ".api-test-missing";
    struct Guarded {
        char before{'L'};
        std::array<char, 8> error{};
        char after{'R'};
    } guarded;
    guarded.error.fill('#');
    CHECK(paper_create(missing.c_str(), guarded.error.data(), guarded.error.size()) == nullptr);
    CHECK(guarded.before == 'L' && guarded.after == 'R');
    CHECK(guarded.error.front() != '\0');
    CHECK(guarded.error.back() == '\0');

    char one = '#';
    CHECK(paper_create(missing.c_str(), &one, 1) == nullptr);
    CHECK(one == '\0');
    char untouched = '#';
    CHECK(paper_create(missing.c_str(), &untouched, 0) == nullptr);
    CHECK(untouched == '#');
    CHECK(paper_create(missing.c_str(), nullptr, 32) == nullptr);
}

void valid_context_and_time(const char* path, const Quote& quote) {
    auto handle = create(path);
    PaperSnapshot snapshot{};
    PaperEvent event{};
    CHECK(paper_snapshot(handle.get(), nullptr) == -1);
    CHECK(paper_pop_event(handle.get(), nullptr) == -1);
    CHECK(paper_snapshot(handle.get(), &snapshot) == 1);
    CHECK(snapshot.position == 0 && snapshot.cash_paise == 0);
    CHECK(snapshot.fees_paise == 0 && snapshot.pnl_paise == 0);
    CHECK(snapshot.bid_paise == 0 && snapshot.ask_paise == 0);
    CHECK(snapshot.open_orders == 0 && snapshot.killed == 0 && snapshot.audit_failed == 0);
    CHECK(paper_pop_event(handle.get(), &event) == 0);

    CHECK(tick(handle.get(), quote) == 1);
    CHECK(paper_snapshot(handle.get(), &snapshot) == 1);
    CHECK(snapshot.bid_paise == quote.price && snapshot.ask_paise == quote.price);
    CHECK(snapshot.killed == 0);
    CHECK(tick(handle.get(), quote) == 0); // Duplicate sequence cannot replenish liquidity.
    CHECK(tick(handle.get(), quote, first_time - 1, 2) == 0);
    CHECK(paper_snapshot(handle.get(), &snapshot) == 1 && snapshot.killed == 0);

    CHECK(paper_heartbeat(handle.get(), first_time + 1) == 1);
    CHECK(paper_heartbeat(handle.get(), first_time) == -1);
    CHECK(paper_snapshot(handle.get(), &snapshot) == 1);
    CHECK(snapshot.killed == 1 && snapshot.open_orders == 0);
    CHECK(drain(handle.get()) >= 1);
    paper_shutdown(handle.get());
    CHECK(paper_snapshot(handle.get(), &snapshot) == 1 && snapshot.open_orders == 0);
}

void invalid_inputs_and_shutdown(const char* path, const Quote& quote) {
    auto invalid_session = create(path);
    CHECK(tick(invalid_session.get(), quote, first_time, 1, -1) == -1);
    PaperSnapshot snapshot{};
    CHECK(paper_snapshot(invalid_session.get(), &snapshot) == 1);
    CHECK(snapshot.killed == 1 && snapshot.open_orders == 0);
    CHECK(drain(invalid_session.get()) >= 1);

    auto invalid_time = create(path);
    CHECK(tick(invalid_time.get(), quote, std::numeric_limits<uint64_t>::max()) == -1);
    CHECK(paper_snapshot(invalid_time.get(), &snapshot) == 1 && snapshot.killed == 1);

    auto stopped = create(path);
    CHECK(tick(stopped.get(), quote) == 1);
    paper_shutdown(stopped.get());
    CHECK(paper_snapshot(stopped.get(), &snapshot) == 1);
    CHECK(snapshot.killed == 1 && snapshot.open_orders == 0);
    CHECK(drain(stopped.get()) >= 1);

    auto disconnected = create(path);
    paper_disconnect(disconnected.get());
    CHECK(paper_snapshot(disconnected.get(), &snapshot) == 1);
    CHECK(snapshot.killed == 1 && snapshot.open_orders == 0);
    CHECK(drain(disconnected.get()) >= 1);
}

} // namespace

int main(int argc, char** argv) {
    CHECK(argc == 2);
    CHECK(paper_api_version() == 1);
    static_assert(sizeof(PaperSnapshot) == 80);
    static_assert(sizeof(PaperEvent) == 120);
    null_guards();
    bounded_errors(argv[1]);
    const auto quote = quote_for(argv[1]);
    valid_context_and_time(argv[1], quote);
    invalid_inputs_and_shutdown(argv[1], quote);
    std::cout << "C ABI tests passed\n";
}
