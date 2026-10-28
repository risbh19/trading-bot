#pragma once

#include <cstdlib>
#include <iostream>

#define CHECK(...) do { \
    if (!(__VA_ARGS__)) { \
        std::cerr << __FILE__ << ':' << __LINE__ << ": " << #__VA_ARGS__ << '\n'; \
        std::exit(1); \
    } \
} while (false)
