#pragma once

#include "trading/types.hpp"

namespace trading {

// Rates in parts per million of turnover. Defaults are illustrative, NOT a tariff.
struct FeeSchedule {
    std::uint32_t brokerage_ppm{300};
    Money brokerage_cap{2'000};
    std::uint32_t stt_buy_ppm{0};
    std::uint32_t stt_sell_ppm{250};
    std::uint32_t exchange_ppm{30};
    std::uint32_t sebi_ppm{1};
    std::uint32_t stamp_buy_ppm{30};
    std::uint32_t gst_ppm{180'000};

    [[nodiscard]] bool valid() const noexcept {
        return brokerage_ppm <= 1'000'000 && brokerage_cap >= 0 && brokerage_cap <= 1'000'000 &&
               stt_buy_ppm <= 1'000'000 && stt_sell_ppm <= 1'000'000 && exchange_ppm <= 1'000'000 &&
               sebi_ppm <= 1'000'000 && stamp_buy_ppm <= 1'000'000 && gst_ppm <= 1'000'000;
    }
};

[[nodiscard]] constexpr Money charge(Money turnover, std::uint32_t ppm) noexcept {
    // Split before multiplying to keep bounded notionals within int64.
    return (turnover / 1'000'000) * ppm + ((turnover % 1'000'000) * ppm + 999'999) / 1'000'000;
}

struct Fees {
    Money brokerage{}, stt{}, exchange{}, sebi{}, stamp{}, gst{};
    [[nodiscard]] Money total() const noexcept { return brokerage + stt + exchange + sebi + stamp + gst; }
};

// Evaluate cumulative order turnover, then debit the delta for each partial fill.
[[nodiscard]] inline Fees fees_for(const FeeSchedule& schedule, Money turnover, Side side) noexcept {
    auto brokerage = charge(turnover, schedule.brokerage_ppm);
    if (brokerage > schedule.brokerage_cap) brokerage = schedule.brokerage_cap;
    const auto exchange = charge(turnover, schedule.exchange_ppm);
    const auto sebi = charge(turnover, schedule.sebi_ppm);
    return {brokerage, charge(turnover, side == Side::buy ? schedule.stt_buy_ppm : schedule.stt_sell_ppm),
            exchange, sebi, side == Side::buy ? charge(turnover, schedule.stamp_buy_ppm) : 0,
            charge(brokerage + exchange + sebi, schedule.gst_ppm)};
}

} // namespace trading
