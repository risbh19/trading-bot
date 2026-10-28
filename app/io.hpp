#pragma once

#include "trading/engine.hpp"
#include <charconv>
#include <fstream>
#include <set>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace trading::io {

inline std::string trim(std::string text) {
    const auto start = text.find_first_not_of(" \t\r\n");
    if (start == std::string::npos) return {};
    return text.substr(start, text.find_last_not_of(" \t\r\n") - start + 1);
}

template <typename T>
T integer(std::string_view text) {
    T value{};
    const auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), value);
    if (error != std::errc{} || end != text.data() + text.size() || text.empty()) {
        throw std::runtime_error("Expected an integer in range: " + std::string(text));
    }
    return value;
}

inline bool boolean(std::string_view text) {
    if (text == "true" || text == "1") return true;
    if (text == "false" || text == "0") return false;
    throw std::runtime_error("Expected true/false or 1/0");
}

inline EngineConfig read_config(const std::string& path) {
    std::ifstream file(path);
    if (!file) throw std::runtime_error("Cannot read config: " + path);
    EngineConfig config;
    std::set<std::string> seen;
    std::string line;
    while (std::getline(file, line)) {
        line = trim(line.substr(0, line.find('#')));
        if (line.empty()) continue;
        const auto equal = line.find('=');
        if (equal == std::string::npos) throw std::runtime_error("Expected key=value: " + line);
        const auto key = trim(line.substr(0, equal));
        const auto value = trim(line.substr(equal + 1));
        if (!seen.insert(key).second) throw std::runtime_error("Duplicate config key: " + key);
        if (key == "mode") {
            if (value != "paper") throw std::runtime_error("Only mode=paper is supported; live routing is disabled");
        }
#define FIELD(name, target) else if (key == name) target = integer<decltype(target)>(value)
        FIELD("instrument_id", config.instrument.id);
        FIELD("initial_capital_paise", config.initial_capital_paise);
        FIELD("tick_size_paise", config.instrument.tick_size);
        FIELD("lot_size", config.instrument.lot_size);
        FIELD("freeze_quantity", config.instrument.freeze_quantity);
        FIELD("lower_band_paise", config.instrument.lower_band);
        FIELD("upper_band_paise", config.instrument.upper_band);
        FIELD("latency_ns", config.paper.latency_ns);
        FIELD("slippage_ticks", config.paper.slippage_ticks);
        FIELD("reject_every", config.paper.reject_every);
        FIELD("max_order_quantity", config.risk.max_order_quantity);
        FIELD("max_position", config.risk.max_position);
        FIELD("max_notional_paise", config.risk.max_total_notional);
        FIELD("max_daily_loss_paise", config.risk.max_daily_loss);
        FIELD("max_open_orders", config.risk.max_open_orders);
        FIELD("orders_per_second", config.risk.orders_per_second);
        FIELD("orders_per_minute", config.risk.orders_per_minute);
        FIELD("orders_per_day", config.risk.orders_per_day);
        FIELD("price_band_bps", config.risk.price_band_bps);
        FIELD("stale_after_ns", config.risk.stale_after_ns);
        FIELD("momentum_ticks", config.strategy.momentum_ticks);
        FIELD("strategy_quantity", config.strategy.quantity);
        FIELD("brokerage_ppm", config.paper.fees.brokerage_ppm);
        FIELD("brokerage_cap_paise", config.paper.fees.brokerage_cap);
        FIELD("stt_buy_ppm", config.paper.fees.stt_buy_ppm);
        FIELD("stt_sell_ppm", config.paper.fees.stt_sell_ppm);
        FIELD("exchange_ppm", config.paper.fees.exchange_ppm);
        FIELD("sebi_ppm", config.paper.fees.sebi_ppm);
        FIELD("stamp_buy_ppm", config.paper.fees.stamp_buy_ppm);
        FIELD("gst_ppm", config.paper.fees.gst_ppm);
#undef FIELD
        else if (key == "allow_short") config.risk.allow_short = boolean(value);
        else if (key == "strategy_enabled") config.strategy_enabled = boolean(value);
        else if (key == "product") {
            if (value == "cnc") config.strategy.product = Product::cnc;
            else if (value == "mis") config.strategy.product = Product::mis;
            else if (value == "nrml") config.strategy.product = Product::nrml;
            else throw std::runtime_error("Unknown product: " + value);
        } else throw std::runtime_error("Unknown config key: " + key);
    }
    if (file.bad()) throw std::runtime_error("Config read failed");
    PaperBroker broker(config.instrument, config.paper);
    if (!broker.valid() || !config.risk.valid() || !config.strategy.valid() ||
        config.initial_capital_paise < 0 || config.initial_capital_paise > notional(max_price, max_quantity) ||
        config.strategy.quantity % config.instrument.lot_size != 0 ||
        config.strategy.quantity > config.risk.max_order_quantity) {
        throw std::runtime_error("Invalid configuration bounds; see config/paper.conf");
    }
    return config;
}

inline Tick parse_tick(const std::string& row) {
    std::vector<std::string> cells;
    std::size_t start{};
    while (true) {
        const auto end = row.find(',', start);
        cells.push_back(trim(row.substr(start, end - start)));
        if (end == std::string::npos) break;
        start = end + 1;
    }
    if (cells.size() != 8) throw std::runtime_error("Tick requires exactly eight CSV columns");
    return {integer<Time>(cells[0]), integer<std::uint64_t>(cells[1]), integer<std::uint32_t>(cells[2]),
            integer<Price>(cells[3]), integer<Price>(cells[4]), integer<Quantity>(cells[5]),
            integer<Quantity>(cells[6]), boolean(cells[7])};
}

inline const char* name(EventType value) noexcept {
    switch (value) {
        case EventType::accepted: return "accepted";
        case EventType::fill: return "fill";
        case EventType::cancelled: return "cancelled";
        case EventType::rejected: return "rejected";
        case EventType::signal: return "signal";
        case EventType::risk: return "risk";
        case EventType::kill: return "kill";
        case EventType::feed: return "feed";
    }
    return "unknown";
}

inline const char* name(Reject value) noexcept {
    switch (value) {
        case Reject::none: return "none";
        case Reject::invalid: return "invalid";
        case Reject::killed: return "killed";
        case Reject::disconnected: return "disconnected";
        case Reject::stale: return "stale";
        case Reject::session_closed: return "session_closed";
        case Reject::size: return "size";
        case Reject::position: return "position";
        case Reject::notional: return "notional";
        case Reject::price_band: return "price_band";
        case Reject::open_orders: return "open_orders";
        case Reject::rate: return "rate";
        case Reject::daily_limit: return "daily_limit";
        case Reject::duplicate: return "duplicate";
        case Reject::capacity: return "capacity";
        case Reject::unknown_order: return "unknown_order";
        case Reject::live_disabled: return "live_disabled";
        case Reject::broker_reject: return "broker_reject";
        case Reject::funds: return "funds";
    }
    return "unknown";
}

inline void write_event(std::ostream& stream, const Event& event) {
    stream << event.time << ',' << event.order_id << ',' << name(event.type) << ','
           << (event.side == Side::buy ? "buy" : "sell") << ',' << static_cast<int>(event.status) << ','
           << name(event.reason) << ',' << event.price << ',' << event.quantity << ',' << event.fee << ','
           << event.position << ',' << event.equity << '\n';
}

} // namespace trading::io
