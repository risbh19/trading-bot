#include "io.hpp"
#include <csignal>
#include <filesystem>
#include <iostream>
#include <memory>

namespace {
volatile std::sig_atomic_t stop_requested = 0;
void request_stop(int) { stop_requested = 1; }

void parent_directory(const std::string& path) {
    const auto parent = std::filesystem::path(path).parent_path();
    if (!parent.empty()) std::filesystem::create_directories(parent);
}

int run(int argc, char** argv) {
    using namespace trading;
    std::string input_path = "data/sample_ticks.csv";
    std::string config_path = "config/paper.conf";
    std::string audit_path = "reports/audit.csv";
    std::string summary_path = "reports/summary.json";
    for (int i = 1; i < argc; ++i) {
        const std::string key = argv[i];
        if (key == "--help") {
            std::cout << "paper_trader [--input ticks.csv] [--config paper.conf] [--audit audit.csv] [--summary summary.json] [--mode paper]\n";
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
        } else throw std::runtime_error("Unknown argument: " + key);
    }
    const std::array paths{input_path, config_path, audit_path, summary_path};
    for (std::size_t a = 0; a < paths.size(); ++a) {
        for (std::size_t b = a + 1; b < paths.size(); ++b) {
            if ((std::filesystem::exists(paths[a]) && std::filesystem::exists(paths[b]) &&
                 std::filesystem::equivalent(paths[a], paths[b])) ||
                std::filesystem::weakly_canonical(paths[a]) == std::filesystem::weakly_canonical(paths[b])) {
                throw std::runtime_error("Input, config, audit and summary paths must be distinct");
            }
        }
    }
    auto engine = std::make_unique<Engine>(io::read_config(config_path));
    std::ifstream input(input_path);
    if (!input) throw std::runtime_error("Cannot read market data: " + input_path);
    std::string line;
    if (!std::getline(input, line) || io::trim(line) !=
        "time_ns,sequence,instrument,bid_paise,ask_paise,bid_quantity,ask_quantity,session_open") {
        throw std::runtime_error("Unexpected market-data CSV header; see data/sample_ticks.csv");
    }
    parent_directory(audit_path);
    parent_directory(summary_path);
    std::ofstream audit(audit_path);
    if (!audit) throw std::runtime_error("Cannot open audit output");
    audit << "time_ns,order_id,event,side,status,reason,price_paise,quantity,fee_paise,position,equity_paise\n";
    std::uint64_t ticks{}, dropped{}, accepted{}, rejected{}, fills{};
    Money peak{}, drawdown{};
    auto drain = [&] {
        Event event;
        while (engine->pop_event(event)) {
            io::write_event(audit, event);
            if (event.type == EventType::accepted) ++accepted;
            if (event.type == EventType::risk || event.type == EventType::rejected) ++rejected;
            if (event.type == EventType::fill) ++fills;
        }
        audit.flush(); // Disk work is outside Engine::on_tick/submit.
        if (!audit || engine->audit_failed()) {
            engine->kill(Reject::capacity);
            throw std::runtime_error("Audit output failed; trading halted");
        }
    };
    std::signal(SIGINT, request_stop);
    std::signal(SIGTERM, request_stop);
    try {
        while (!stop_requested && std::getline(input, line)) {
            if (io::trim(line).empty()) continue;
            const Tick tick = io::parse_tick(line); // Parsing/allocation occurs before the hot path.
            ++ticks;
            if (!engine->on_tick(tick)) ++dropped;
            peak = std::max(peak, engine->equity());
            drawdown = std::max(drawdown, peak - engine->equity());
            drain();
            if (engine->killed()) break;
        }
        if (input.bad()) throw std::runtime_error("Market-data read failed");
        if (ticks == 0) throw std::runtime_error("Market-data CSV contains no ticks");
    } catch (...) {
        engine->kill(Reject::invalid);
        drain();
        throw;
    }
    const bool risk_halted = engine->killed();
    const auto reason = engine->kill_reason();
    engine->shutdown();
    drain();
    std::ofstream summary(summary_path);
    if (!summary) throw std::runtime_error("Cannot open summary output");
    summary << "{\n  \"mode\": \"paper\",\n  \"ticks\": " << ticks
            << ",\n  \"dropped_ticks\": " << dropped << ",\n  \"orders_accepted\": " << accepted
            << ",\n  \"orders_rejected\": " << rejected << ",\n  \"fills\": " << fills
            << ",\n  \"position\": " << engine->position().quantity << ",\n  \"cash_paise\": " << engine->position().cash
            << ",\n  \"fees_paise\": " << engine->position().fees << ",\n  \"pnl_paise\": " << engine->equity()
            << ",\n  \"max_drawdown_paise\": " << drawdown << ",\n  \"risk_halted\": " << (risk_halted ? "true" : "false")
            << ",\n  \"stop_reason\": \"" << (stop_requested ? "interrupted" : risk_halted ? io::name(reason) : "end_of_replay")
            << "\",\n  \"open_orders\": " << engine->open_orders() << "\n}\n";
    summary.flush();
    if (!summary) throw std::runtime_error("Summary write failed");
    std::cout << "Paper replay complete: " << ticks << " ticks, " << fills << " fills, position "
              << engine->position().quantity << ", PnL " << engine->equity() << " paise, fees "
              << engine->position().fees << " paise\nAudit: " << audit_path << "\nSummary: " << summary_path << '\n';
    return risk_halted ? 3 : 0;
}
} // namespace

int main(int argc, char** argv) {
    try { return run(argc, argv); }
    catch (const std::exception& error) { std::cerr << "Error: " << error.what() << '\n'; return 2; }
}
