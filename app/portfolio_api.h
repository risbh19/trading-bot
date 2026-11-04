#ifndef TRADING_PORTFOLIO_API_H
#define TRADING_PORTFOLIO_API_H

#include "paper_api.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Additive portfolio ABI 1. The existing single-engine ABI remains version 2.
   Each opaque context has exactly one owner thread. All execution is paper-only. */
typedef struct PortfolioSnapshot {
    int64_t initial_capital_paise;
    int64_t cash_paise;
    int64_t equity_paise;
    int64_t pnl_paise;
    int64_t fees_paise;
    int64_t gross_notional_paise;
    int64_t reserved_cash_paise;
    int64_t open_orders;
    int64_t killed;
    int64_t audit_failed;
    int64_t kill_reason;
    int64_t instrument_count;
    int64_t ready;
    int64_t execution_enabled;
} PortfolioSnapshot;

typedef struct PortfolioInstrumentSnapshot {
    int64_t instrument;
    int64_t ready;
    PaperSnapshot state;
} PortfolioInstrumentSnapshot;

typedef struct PortfolioAuditEvent {
    int64_t instrument;
    PaperEvent event;
} PortfolioAuditEvent;

PAPER_API int portfolio_api_version(void) PAPER_NOEXCEPT;
PAPER_API void* portfolio_create(const char* config_path, char* error, size_t capacity) PAPER_NOEXCEPT;
PAPER_API void portfolio_destroy(void* context) PAPER_NOEXCEPT;
/* 1 accepted, 0 duplicate/regressing per-instrument snapshot, -1 invalid. */
PAPER_API int portfolio_tick(void* context, uint64_t time_ns, uint64_t sequence, uint32_t instrument,
                             int64_t bid, int64_t ask, int32_t quantity, int32_t session_open) PAPER_NOEXCEPT;
PAPER_API int portfolio_heartbeat(void* context, uint64_t time_ns) PAPER_NOEXCEPT;
/* Suspend advances the heartbeat and cancels ALL pending virtual orders. */
PAPER_API int portfolio_suspend(void* context, uint64_t time_ns) PAPER_NOEXCEPT;
/* Resume never clears a kill latch. 1 enabled, 0 barrier/health not ready, -1 invalid. */
PAPER_API int portfolio_resume(void* context, uint64_t time_ns) PAPER_NOEXCEPT;
PAPER_API void portfolio_disconnect(void* context) PAPER_NOEXCEPT;
PAPER_API void portfolio_shutdown(void* context) PAPER_NOEXCEPT;
PAPER_API int portfolio_snapshot(void* context, PortfolioSnapshot* output) PAPER_NOEXCEPT;
PAPER_API int portfolio_instrument_snapshot(void* context, size_t index,
                                            PortfolioInstrumentSnapshot* output) PAPER_NOEXCEPT;
PAPER_API int portfolio_pop_event(void* context, PortfolioAuditEvent* output) PAPER_NOEXCEPT;

#ifdef __cplusplus
}
#endif
#endif
