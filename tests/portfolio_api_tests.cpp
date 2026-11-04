#include "../app/portfolio_api.h"
#include "test_support.hpp"
#include <cstring>
#include <limits>

int main(int argc, char** argv) {
    CHECK(argc == 2);
    CHECK(paper_api_version() == 2);
    CHECK(portfolio_api_version() == 1);
    CHECK(sizeof(PortfolioSnapshot) == 112);
    CHECK(sizeof(PortfolioInstrumentSnapshot) == 96);
    CHECK(sizeof(PortfolioAuditEvent) == 128);
    char error[1024]{};
    CHECK(portfolio_create(nullptr, error, sizeof(error)) == nullptr);
    CHECK(error[0] != '\0');
    CHECK(portfolio_heartbeat(nullptr, 0) == -1);
    CHECK(portfolio_suspend(nullptr, 0) == -1);
    CHECK(portfolio_resume(nullptr, 0) == -1);
    PortfolioSnapshot state{};
    CHECK(portfolio_snapshot(nullptr, &state) == -1);
    auto* context = portfolio_create(argv[1], error, sizeof(error));
    CHECK(context != nullptr);
    CHECK(portfolio_snapshot(context, &state) == 1);
    CHECK(state.instrument_count == 2 && state.initial_capital_paise == 10'000'000);
    CHECK(!state.ready && state.execution_enabled);
    PortfolioInstrumentSnapshot member{};
    CHECK(portfolio_instrument_snapshot(context, 0, &member) == 1 && member.instrument == 1);
    CHECK(portfolio_instrument_snapshot(context, 2, &member) == -1);
    CHECK(portfolio_snapshot(context, nullptr) == -1);
    CHECK(portfolio_suspend(context, 0) == 1);
    CHECK(portfolio_resume(context, 0) == 0); // All constituents must initialize.
    CHECK(portfolio_tick(context, 0, 1, 1, 10'000, 10'010, 10, 1) == 1);
    CHECK(portfolio_tick(context, 0, 1, 2, 10'200, 10'210, 10, 1) == 1);
    CHECK(portfolio_snapshot(context, &state) == 1 && state.ready && !state.execution_enabled);
    CHECK(portfolio_resume(context, 1) == 1);
    CHECK(portfolio_tick(context, 10'000'000, 2, 1, 10'020, 10'030, 10, 1) == 1);
    CHECK(portfolio_snapshot(context, &state) == 1 && state.open_orders == 1);
    CHECK(portfolio_suspend(context, 10'000'001) == 1);
    CHECK(portfolio_snapshot(context, &state) == 1 && state.open_orders == 0 && !state.killed);
    CHECK(portfolio_tick(context, 20'000'000, 2, 2, 10'220, 10'230, 10, 1) == 1);
    CHECK(portfolio_snapshot(context, &state) == 1 && state.open_orders == 0 && !state.execution_enabled);
    PortfolioAuditEvent event{};
    bool cancelled = false;
    while (portfolio_pop_event(context, &event) == 1) {
        if (std::strcmp(event.event.event, "cancelled") == 0) {
            CHECK(event.instrument == 1);
            cancelled = true;
        }
    }
    CHECK(cancelled);
    CHECK(portfolio_resume(context, 20'000'001) == 1);
    CHECK(portfolio_heartbeat(context, 3'000'000'000) == 1);
    CHECK(portfolio_snapshot(context, &state) == 1 && state.killed && !state.execution_enabled);
    CHECK(state.kill_reason == 4); // Stale remains latched.
    CHECK(portfolio_resume(context, 3'000'000'000) == 0);
    portfolio_destroy(context);

    context = portfolio_create(argv[1], error, sizeof(error));
    CHECK(context != nullptr);
    CHECK(portfolio_tick(context, 0, 1, 99, 10'000, 10'010, 10, 1) == -1);
    CHECK(portfolio_snapshot(context, &state) == 1 && state.killed && state.kill_reason == 1);
    portfolio_destroy(context);
    context = portfolio_create(argv[1], error, sizeof(error));
    CHECK(context != nullptr);
    CHECK(portfolio_heartbeat(context, std::numeric_limits<uint64_t>::max()) == -1);
    CHECK(portfolio_snapshot(context, &state) == 1 && state.killed);
    portfolio_destroy(context);
    portfolio_destroy(nullptr);
}
