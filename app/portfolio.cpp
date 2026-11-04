#include "portfolio_io.hpp"
#include <csignal>
#include <iostream>

namespace {
volatile std::sig_atomic_t stop_requested = 0;
void request_stop(int) { stop_requested = 1; }

void parent_directory(const std::string& path) {
    const auto parent = std::filesystem::path(path).parent_path();
    if (!parent.empty()) std::filesystem::create_directories(parent);
}

int run(int argc, char** argv) {
    using namespace trading;
    std::string input_path = "data/portfolio_ticks.csv";
    std::string config_path = "config/portfolio.conf";
    std::string audit_path = "reports/portfolio/audit.csv";
    std::string summary_path = "reports/portfolio/summary.json";
    for (int i = 1; i < argc; ++i) {
        const std::string key = argv[i];
        if (key == "--help") {
            std::cout << "paper_portfolio [--input mixed-ticks.csv] [--config portfolio.conf] "
                         "[--audit audit.csv] [--summary summary.json] [--mode paper]\n";
            return 0;
        }
        if (i + 1 == argc) throw std::runtime_error("Missing value for " + key);
        const std::string value = argv[++i];
        if (key == "--input") input_path = value;
        else if (key == "--config") config_path = value;
        else if (key == "--audit") audit_path = value;
        else if (key == "--summary") summary_path = value;
        else if (key == "--mode") {
            if (value != "paper") throw std::runtime_error("Live mode is disabled; only --mode paper is accepted");
        } else throw std::runtime_error("Unknown portfolio argument: " + key);
    }
    auto settings = io::read_portfolio(config_path);
    auto paths = settings.sources;
    paths.insert(paths.end(), {input_path, audit_path, summary_path, argv[0]});
    io::distinct_paths(paths);
    if (std::filesystem::exists(audit_path) || std::filesystem::exists(summary_path)) {
        throw std::runtime_error("Portfolio outputs already exist; choose new audit/summary paths");
    }
    auto portfolio = std::make_unique<PaperPortfolio>(settings.account, settings.instruments);
    std::ifstream input(input_path);
    if (!input) throw std::runtime_error("Cannot read portfolio market data: " + input_path);
    std::string line;
    if (!std::getline(input, line) || io::trim(line) !=
        "time_ns,sequence,instrument,bid_paise,ask_paise,bid_quantity,ask_quantity,session_open") {
        throw std::runtime_error("Unexpected portfolio CSV header");
    }
    parent_directory(audit_path);
    parent_directory(summary_path);
    std::ofstream audit(audit_path);
    if (!audit) throw std::runtime_error("Cannot open portfolio audit");
    audit << "instrument,time_ns,order_id,event,side,status,reason,price_paise,quantity,fee_paise,position,equity_paise\n";
    std::uint64_t ticks{}, dropped{}, accepted{}, rejected{}, fills{};
    Money peak{}, drawdown{};
    auto drain = [&] {
        PortfolioEvent tagged;
        while (portfolio->pop_event(tagged)) {
            audit << tagged.instrument << ',';
            io::write_event(audit, tagged.event);
            if (tagged.event.type == EventType::accepted) ++accepted;
            if (tagged.event.type == EventType::risk || tagged.event.type == EventType::rejected) ++rejected;
            if (tagged.event.type == EventType::fill) ++fills;
        }
        audit.flush();
        if (!audit || portfolio->audit_failed()) {
            portfolio->kill(Reject::capacity);
            throw std::runtime_error("Portfolio audit failed; all paper orders cancelled");
        }
    };
    std::signal(SIGINT, request_stop);
    std::signal(SIGTERM, request_stop);
    try {
        while (!stop_requested && std::getline(input, line)) {
            if (io::trim(line).empty()) continue;
            ++ticks;
            if (!portfolio->on_tick(io::parse_tick(line))) ++dropped;
            peak = std::max(peak, portfolio->pnl());
            drawdown = std::max(drawdown, peak - portfolio->pnl());
            drain();
            if (portfolio->killed()) break;
        }
        if (input.bad() || ticks == 0) throw std::runtime_error("Portfolio input failed or contains no ticks");
        if (!portfolio->ready() && !portfolio->killed() && !stop_requested) {
            throw std::runtime_error("Portfolio input never initialized every configured instrument");
        }
    } catch (...) {
        portfolio->kill(Reject::invalid);
        drain();
        throw;
    }
    const bool halted = portfolio->killed();
    const auto reason = portfolio->kill_reason();
    portfolio->shutdown();
    drain();
    std::ofstream summary(summary_path);
    if (!summary) throw std::runtime_error("Cannot open portfolio summary");
    summary << "{\n  \"mode\": \"paper\",\n  \"initial_capital_paise\": " << portfolio->initial_capital()
            << ",\n  \"ticks\": " << ticks << ",\n  \"dropped_ticks\": " << dropped
            << ",\n  \"orders_accepted\": " << accepted << ",\n  \"orders_rejected\": " << rejected
            << ",\n  \"fills\": " << fills << ",\n  \"virtual_cash_paise\": " << portfolio->cash()
            << ",\n  \"virtual_equity_paise\": " << portfolio->equity()
            << ",\n  \"pnl_paise\": " << portfolio->pnl() << ",\n  \"fees_paise\": " << portfolio->fees()
            << ",\n  \"gross_notional_paise\": " << portfolio->gross_notional()
            << ",\n  \"reserved_cash_paise\": " << portfolio->reserved_cash()
            << ",\n  \"open_orders\": " << portfolio->open_orders()
            << ",\n  \"max_drawdown_paise\": " << drawdown
            << ",\n  \"risk_halted\": " << (halted ? "true" : "false")
            << ",\n  \"stop_reason\": \"" << (stop_requested ? "interrupted" : halted ? io::name(reason) : "end_of_replay")
            << "\",\n  \"terminal_inventory_is_marked_not_liquidated\": true,\n  \"instruments\": [";
    for (std::size_t index = 0; index < portfolio->size(); ++index) {
        const auto& engine = portfolio->engine(index);
        summary << (index == 0 ? "\n" : ",\n")
                << "    {\"instrument\": " << portfolio->config(index).instrument.id
                << ", \"ready\": " << (engine.book().ready() ? "true" : "false")
                << ", \"position\": " << engine.position().quantity
                << ", \"cash_flow_paise\": " << engine.position().cash
                << ", \"fees_paise\": " << engine.position().fees
                << ", \"pnl_paise\": " << engine.equity() << "}";
    }
    summary << "\n  ]\n}\n";
    summary.flush();
    if (!summary) throw std::runtime_error("Portfolio summary write failed");
    std::cout << "PAPER portfolio: " << ticks << " ticks across " << portfolio->size()
              << " instruments, shared cash " << portfolio->cash()
              << " paise, marked PnL " << portfolio->pnl() << " paise\n";
    return halted ? 3 : 0;
}
} // namespace

int main(int argc, char** argv) {
    try { return run(argc, argv); }
    catch (const std::exception& error) { std::cerr << "Error: " << error.what() << '\n'; return 2; }
}
