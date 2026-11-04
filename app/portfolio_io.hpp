#pragma once

#include "io.hpp"
#include "trading/paper_portfolio.hpp"
#include <filesystem>

namespace trading::io {

struct PortfolioInput {
    PortfolioConfig account;
    std::vector<EngineConfig> instruments;
    std::vector<std::string> sources;
};

inline PortfolioInput read_portfolio(const std::string& path) {
    std::ifstream file(path);
    if (!file) throw std::runtime_error("Cannot read portfolio config: " + path);
    PortfolioInput result;
    result.sources.push_back(path);
    std::set<std::string> seen;
    std::string line;
    while (std::getline(file, line)) {
        line = trim(line.substr(0, line.find('#')));
        if (line.empty()) continue;
        const auto equal = line.find('=');
        if (equal == std::string::npos) throw std::runtime_error("Expected portfolio key=value");
        const auto key = trim(line.substr(0, equal));
        const auto value = trim(line.substr(equal + 1));
        if (key != "instrument_config" && !seen.insert(key).second) {
            throw std::runtime_error("Duplicate portfolio config key: " + key);
        }
        if (key == "instrument_config") {
            if (value.empty() || result.instruments.size() >= PaperPortfolio::max_instruments) {
                throw std::runtime_error("Portfolio needs 1..32 instrument configurations");
            }
            const auto source = (std::filesystem::path(path).parent_path() / value).string();
            result.instruments.push_back(read_config(source));
            result.sources.push_back(source);
        } else if (key == "mode") {
            if (value != "paper") throw std::runtime_error("Live portfolio mode is disabled");
        }
#define ACCOUNT(name, target) else if (key == name) target = integer<decltype(target)>(value)
        ACCOUNT("initial_capital_paise", result.account.initial_capital);
        ACCOUNT("max_gross_notional_paise", result.account.max_gross_notional);
        ACCOUNT("max_daily_loss_paise", result.account.max_loss);
        ACCOUNT("max_open_orders", result.account.max_open_orders);
        ACCOUNT("orders_per_second", result.account.orders_per_second);
        ACCOUNT("orders_per_minute", result.account.orders_per_minute);
        ACCOUNT("orders_per_session", result.account.orders_per_session);
#undef ACCOUNT
        else throw std::runtime_error("Unknown portfolio config key: " + key);
    }
    if (file.bad()) throw std::runtime_error("Portfolio config read failed");
    return result;
}

inline void distinct_paths(const std::vector<std::string>& paths) {
    for (std::size_t a = 0; a < paths.size(); ++a) {
        for (std::size_t b = a + 1; b < paths.size(); ++b) {
            if ((std::filesystem::exists(paths[a]) && std::filesystem::exists(paths[b]) &&
                 std::filesystem::equivalent(paths[a], paths[b])) ||
                std::filesystem::weakly_canonical(paths[a]) == std::filesystem::weakly_canonical(paths[b])) {
                throw std::runtime_error("Portfolio input, configs, executable and outputs must be distinct");
            }
        }
    }
}

} // namespace trading::io
