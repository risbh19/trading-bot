#include "trading/order_book.hpp"
#include "trading/spsc_queue.hpp"
#include "test_support.hpp"

#include <limits>
#include <thread>

int main() {
    using namespace trading;
    CHECK(notional(12'345, 12) == 148'140);
    CHECK(!checked_add(std::numeric_limits<Money>::max(), 1));
    CHECK(!checked_add(std::numeric_limits<Money>::min(), -1));
    CHECK(checked_add(-5, 10) == 5);

    OrderBook book;
    Tick tick{100, 1, 1, 10'000, 10'010, 10, 20, true};
    CHECK(book.update(tick));
    CHECK(book.mid() == 10'005);
    CHECK(!book.update(tick));
    tick.sequence = 2;
    tick.bid = 10'015;
    CHECK(!book.update(tick));
    CHECK(book.tick().sequence == 1);
    tick.bid = 10'000;
    tick.time = 99;
    CHECK(!book.update(tick));
    tick.time = 100;
    tick.bid = 10'001;
    CHECK(!book.update(tick));
    tick.bid = 10'000;
    tick.instrument = 2;
    CHECK(!book.update(tick));

    SpscQueue<int, 4> small;
    int value{};
    CHECK(!small.pop(value));
    for (int i = 0; i < 4; ++i) CHECK(small.push(i));
    CHECK(!small.push(5));
    for (int i = 0; i < 4; ++i) { CHECK(small.pop(value)); CHECK(value == i); }
    for (int i = 0; i < 100; ++i) { CHECK(small.push(i)); CHECK(small.pop(value)); CHECK(value == i); }

    SpscQueue<std::uint64_t, 256> threaded;
    constexpr std::uint64_t count = 100'000;
    std::thread producer([&] {
        for (std::uint64_t i = 0; i < count; ++i) {
            while (!threaded.push(i)) std::this_thread::yield();
        }
    });
    for (std::uint64_t i = 0; i < count; ++i) {
        std::uint64_t received{};
        while (!threaded.pop(received)) std::this_thread::yield();
        CHECK(received == i);
    }
    producer.join();
    std::cout << "Core tests passed\n";
}
