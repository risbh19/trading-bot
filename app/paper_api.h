#ifndef TRADING_PAPER_API_H
#define TRADING_PAPER_API_H

#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32)
#if defined(PAPER_ENGINE_BUILD)
#define PAPER_API __declspec(dllexport)
#else
#define PAPER_API __declspec(dllimport)
#endif
#elif defined(__GNUC__)
#define PAPER_API __attribute__((visibility("default")))
#else
#define PAPER_API
#endif

#ifdef __cplusplus
#define PAPER_NOEXCEPT noexcept
extern "C" {
#else
#define PAPER_NOEXCEPT
#endif

/* ABI version 1. Each context has one owner; concurrent calls are unsupported. */
typedef struct PaperSnapshot {
    int64_t position;
    int64_t cash_paise;
    int64_t fees_paise;
    int64_t pnl_paise;
    int64_t bid_paise;
    int64_t ask_paise;
    int64_t open_orders;
    int64_t killed;
    int64_t audit_failed;
    int64_t kill_reason;
} PaperSnapshot;

typedef struct PaperEvent {
    int64_t time_ns;
    int64_t order_id;
    int64_t side;
    int64_t status;
    int64_t price_paise;
    int64_t quantity;
    int64_t fee_paise;
    int64_t position;
    int64_t equity_paise;
    char event[16];
    char reason[32];
} PaperEvent;

PAPER_API int paper_api_version(void) PAPER_NOEXCEPT;
/* Returns NULL on error and writes a bounded, NUL-terminated error if provided. */
PAPER_API void* paper_create(const char* config_path, char* error, size_t error_capacity) PAPER_NOEXCEPT;
PAPER_API void paper_destroy(void* context) PAPER_NOEXCEPT;
/* Returns 1 accepted, 0 duplicate/regressing, -1 invalid. Time must fit int64_t. */
PAPER_API int paper_tick(void* context, uint64_t time_ns, uint64_t sequence,
                         int64_t bid, int64_t ask, int32_t bid_qty, int32_t ask_qty,
                         int32_t session_open) PAPER_NOEXCEPT;
/* Returns 1 processed, -1 for NULL context, regressing time, or out-of-range time. */
PAPER_API int paper_heartbeat(void* context, uint64_t time_ns) PAPER_NOEXCEPT;
PAPER_API void paper_disconnect(void* context) PAPER_NOEXCEPT;
PAPER_API void paper_shutdown(void* context) PAPER_NOEXCEPT;
/* Returns 1 on success, -1 for NULL context/output. PnL excludes initial capital. */
PAPER_API int paper_snapshot(void* context, PaperSnapshot* output) PAPER_NOEXCEPT;
/* Returns 1 for an event, 0 when empty, -1 for NULL context/output. */
PAPER_API int paper_pop_event(void* context, PaperEvent* output) PAPER_NOEXCEPT;

#ifdef __cplusplus
}
#endif

#endif
