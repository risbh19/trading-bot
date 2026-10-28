#pragma once

#include <array>
#include <atomic>
#include <cstddef>
#include <type_traits>

namespace trading {

// Exactly one producer and one consumer. N slots are usable; no sentinel slot.
template <typename T, std::size_t N>
class SpscQueue {
    static_assert(N >= 2 && (N & (N - 1)) == 0, "Capacity must be a power of two");
    static_assert(std::is_trivially_copyable_v<T>);
    static_assert(std::atomic<std::size_t>::is_always_lock_free);

public:
    [[nodiscard]] bool push(const T& value) noexcept {
        const auto head = head_.load(std::memory_order_relaxed);
        if (head - tail_.load(std::memory_order_acquire) == N) {
            return false;
        }
        values_[head & (N - 1)] = value;
        head_.store(head + 1, std::memory_order_release);
        return true;
    }

    [[nodiscard]] bool pop(T& value) noexcept {
        const auto tail = tail_.load(std::memory_order_relaxed);
        if (tail == head_.load(std::memory_order_acquire)) {
            return false;
        }
        value = values_[tail & (N - 1)];
        tail_.store(tail + 1, std::memory_order_release);
        return true;
    }

private:
    alignas(64) std::atomic<std::size_t> head_{};
    alignas(64) std::atomic<std::size_t> tail_{};
    alignas(64) std::array<T, N> values_{};
};

} // namespace trading
