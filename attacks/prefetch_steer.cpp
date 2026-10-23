// Open-loop, single-threaded memory-pressure actuator for ChampSim traces.
#include <array>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

// The Pin tracer recognizes these symbols. Keep them visible and uninlined.
extern "C" __attribute__((noinline)) void prefetchsteer_roi_begin() {
    asm volatile("" ::: "memory");
}
extern "C" __attribute__((noinline)) void prefetchsteer_roi_end() {
    asm volatile("" ::: "memory");
}
extern "C" __attribute__((noinline)) void prefetchsteer_phase(uint64_t phase,
                                                            uint64_t pulse) {
    asm volatile("" : : "r"(phase), "r"(pulse) : "memory");
}

struct Config {
    std::string mode = "stream";
    uint64_t size_mib = 128, streams = 4, seed = 1, write_every = 0;
    uint64_t gap_rounds = 0;
    uint64_t burst = 3;  // sawtooth: sequential lines per burst before a random jump
    uint64_t pre = 1000000, on = 250000, off = 250000, pulses = 4, post = 1000000;
    std::vector<uint64_t> on_sequence;
};

static uint64_t number(const std::string& text) {
    if (text.empty() || text.find_first_not_of("0123456789") != std::string::npos)
        throw std::runtime_error("expected an unsigned decimal integer: " + text);
    size_t used = 0;
    auto value = std::stoull(text, &used);
    if (used != text.size()) throw std::runtime_error("invalid integer: " + text);
    return value;
}

static std::vector<uint64_t> numbers(const std::string& text) {
    std::vector<uint64_t> result;
    size_t begin = 0;
    while (begin <= text.size()) {
        const auto end = text.find(',', begin);
        const auto item = text.substr(begin, end == std::string::npos ? end : end - begin);
        const auto value = number(item);
        if (!value || value > 1000000000000ULL)
            throw std::runtime_error("on-sequence entries must be in [1, 1000000000000]");
        result.push_back(value);
        if (end == std::string::npos) break;
        begin = end + 1;
    }
    return result;
}

static Config parse(int argc, char** argv) {
    Config c;
    for (int i = 1; i < argc; ++i) {
        const std::string key = argv[i];
        if (key == "--help") {
            std::cout << "PrefetchSteer: quiet -> (pressure -> quiet)* -> cooldown\n"
                "  --mode stream|random|chase|quiet|sawtooth  (default stream)\n"
                "  --burst N          sawtooth: sequential lines per burst before a\n"
                "                     random jump (makes Pythia over-prefetch); default 3\n"
                "  --size-mib N       power of two, default 128\n"
                "  --streams N        1, 2, 4, 8 or 16; default 4\n"
                "  --seed N           nonzero deterministic seed, default 1\n"
                "  --write-every N    write each Nth pressure batch; 0=read only\n"
                "  --gap-rounds N     arithmetic rounds between pressure batches; default 0\n"
                "  --pre N            initial arithmetic rounds, default 1000000\n"
                "  --on N             batches per pressure pulse, default 250000\n"
                "  --on-sequence A,B  batches for each pulse; overrides --on\n"
                "  --off N            arithmetic rounds after EVERY pulse, default 250000\n"
                "  --pulses N         number of pressure pulses, default 4\n"
                "  --post N           final arithmetic rounds, default 1000000\n"
                "One batch reads one cache line per stream. Quiet mode replaces\n"
                "each pressure batch with one arithmetic round. Counts are NOT cycles\n"
                "or victim instructions. Pin records exact attacker trace boundaries.\n";
            std::exit(0);
        }
        if (++i == argc) throw std::runtime_error("missing value for " + key);
        if (key == "--mode") { c.mode = argv[i]; continue; }
        if (key == "--on-sequence") { c.on_sequence = numbers(argv[i]); continue; }
        const auto v = number(argv[i]);
        if (key == "--size-mib") c.size_mib = v;
        else if (key == "--streams") c.streams = v;
        else if (key == "--seed") c.seed = v;
        else if (key == "--write-every") c.write_every = v;
        else if (key == "--burst") c.burst = v;
        else if (key == "--gap-rounds") c.gap_rounds = v;
        else if (key == "--pre") c.pre = v;
        else if (key == "--on") c.on = v;
        else if (key == "--off") c.off = v;
        else if (key == "--pulses") c.pulses = v;
        else if (key == "--post") c.post = v;
        else throw std::runtime_error("unknown option: " + key);
    }
    if (c.mode != "stream" && c.mode != "random" && c.mode != "chase" && c.mode != "quiet" &&
        c.mode != "sawtooth")
        throw std::runtime_error("mode must be stream, random, chase, quiet or sawtooth");
    if (c.mode == "sawtooth" && (!c.burst || c.burst > 1048576))
        throw std::runtime_error("burst must be in [1, 1048576]");
    if (!c.size_mib || (c.size_mib & (c.size_mib - 1)) || c.size_mib > 16384)
        throw std::runtime_error("size-mib must be a power of two in [1, 16384]");
    if (!c.streams || (c.streams & (c.streams - 1)) || c.streams > 16)
        throw std::runtime_error("streams must be 1, 2, 4, 8 or 16");
    if (!c.seed) throw std::runtime_error("seed must be nonzero");
    if (c.mode == "chase" && c.write_every)
        throw std::runtime_error("chase is read-only (writes would corrupt the chain)");
    if (c.pulses > 1000000 || c.pre > 1000000000000ULL || c.on > 1000000000000ULL ||
        c.off > 1000000000000ULL || c.post > 1000000000000ULL ||
        c.gap_rounds > 1000000000000ULL)
        throw std::runtime_error("schedule exceeds supported bounds");
    if (!c.on_sequence.empty() && c.on_sequence.size() != c.pulses)
        throw std::runtime_error("on-sequence must have exactly one batch count per pulse");
    if (!c.pre && !c.post && (!c.pulses || (!c.on && c.on_sequence.empty() && !c.off)))
        throw std::runtime_error("schedule has no work");
    return c;
}

static uint64_t random_next(uint64_t& x) {
    x ^= x << 13; x ^= x >> 7; x ^= x << 17;
    return x;
}

// No memory operations in the loop, and no sleep/syscalls lost on trace replay.
__attribute__((always_inline)) static inline uint64_t arithmetic(uint64_t rounds, uint64_t value) {
    if (rounds) {
        asm volatile("1: addq $17, %0\n\trolq $7, %0\n\t"
                     "decq %1\n\tjnz 1b"
                     : "+r"(value), "+r"(rounds) : : "cc");
    }
    return value;
}

__attribute__((noinline)) static uint64_t quiet(uint64_t rounds, uint64_t value) {
    return arithmetic(rounds, value);
}

struct alignas(64) Line { uint64_t value; unsigned char padding[56]; };
static_assert(sizeof(Line) == 64, "ChampSim uses 64-byte cache lines");

template <size_t Streams> class Actuator {
    const Config& c;
    std::unique_ptr<Line[]> storage;
    volatile Line* lines;
    const uint64_t per_stream;
    std::array<uint64_t, Streams> position{}, rng{}, sums{}, burst_at{};
public:
    explicit Actuator(const Config& config)
        : c(config), storage(new Line[c.size_mib * 1024 * 1024 / sizeof(Line)]),
          lines(storage.get()), per_stream(c.size_mib * 1024 * 1024 / sizeof(Line) / Streams) {
        // Commit pages and build any pointer chains BEFORE entering the traced ROI.
        uint64_t seed = c.seed;
        for (uint64_t i = 0; i < per_stream * Streams; ++i)
            lines[i].value = random_next(seed);
        for (size_t s = 0; s < Streams; ++s) {
            rng[s] = random_next(seed);
            if (c.mode != "chase") continue;
            std::vector<uint64_t> permutation(per_stream);
            for (uint64_t i = 0; i < per_stream; ++i) permutation[i] = i;
            for (uint64_t i = per_stream - 1; i > 0; --i)
                std::swap(permutation[i], permutation[random_next(seed) % (i + 1)]);
            for (uint64_t i = 0; i < per_stream; ++i)
                lines[s * per_stream + permutation[i]].value = permutation[(i + 1) & (per_stream - 1)];
            position[s] = permutation[0];
        }
    }

    template <int Mode, bool Throttle> __attribute__((noinline)) void pressure(uint64_t batches) {
        const auto mask = per_stream - 1;
        const auto span = per_stream;
        const auto write_every = c.write_every;
        const auto burst_len = c.burst;
        auto* const buffer = lines;
        auto offsets = position;
        auto random_state = rng;
        auto totals = sums;
        auto burst_pos = burst_at;
        uint64_t delay_state = c.seed;
        for (uint64_t batch = 0; batch < batches; ++batch) {
            const bool write = write_every && batch % write_every == 0;
            #pragma GCC unroll 16
            for (size_t s = 0; s < Streams; ++s) {
                if (Mode == 1) offsets[s] = random_next(random_state[s]) & mask;
                const auto index = s * span + offsets[s];
                const uint64_t value = buffer[index].value; // always a real load
                totals[s] += value;
                if (write) buffer[index].value = value + 1;
                if (Mode == 2) offsets[s] = value; // dependent pointer chain
                else if (Mode == 0) offsets[s] = (offsets[s] + 1) & mask;
                else if (Mode == 3) {
                    // Sawtooth: a short sequential run (which Pythia learns and
                    // over-prefetches) then a random jump that strands the
                    // prefetched look-ahead lines -> wasted bandwidth.
                    if (++burst_pos[s] < burst_len) offsets[s] = (offsets[s] + 1) & mask;
                    else { burst_pos[s] = 0; offsets[s] = random_next(random_state[s]) & mask; }
                }
            }
            // Keep pacing in the instruction trace. No host timers or sleeps.
            if constexpr (Throttle) {
                if (batch + 1 < batches)
                    delay_state = arithmetic(c.gap_rounds, delay_state);
            }
        }
        position = offsets;
        rng = random_state;
        sums = totals;
        burst_at = burst_pos;
    }

    template <int Mode> void pulse(uint64_t batches) {
        // Specialize away pacing overhead in the default saturation workload.
        if (c.gap_rounds) pressure<Mode, true>(batches);
        else pressure<Mode, false>(batches);
    }

    uint64_t run() {
        uint64_t checksum = c.seed;
        prefetchsteer_roi_begin();
        if (c.pre) {
            prefetchsteer_phase(0, 0);
            checksum = quiet(c.pre, checksum);
        }
        for (uint64_t pulse = 0; pulse < c.pulses; ++pulse) {
            const uint64_t batches = c.on_sequence.empty() ? c.on : c.on_sequence[pulse];
            if (batches) {
                prefetchsteer_phase(c.mode == "quiet" ? 4 : 1, pulse);
                if (c.mode == "stream") this->pulse<0>(batches);
                else if (c.mode == "random") this->pulse<1>(batches);
                else if (c.mode == "chase") this->pulse<2>(batches);
                else if (c.mode == "sawtooth") this->pulse<3>(batches);
                else checksum = quiet(batches, checksum);
            }
            if (c.off) {
                prefetchsteer_phase(2, pulse);
                checksum = quiet(c.off, checksum);
            }
        }
        if (c.post) {
            prefetchsteer_phase(3, c.pulses);
            checksum = quiet(c.post, checksum);
        }
        prefetchsteer_roi_end();
        for (auto sum : sums) checksum ^= sum;
        return checksum;
    }
};

int main(int argc, char** argv) {
    try {
        const auto c = parse(argc, argv);
        uint64_t checksum = 0;
        switch (c.streams) {
            case 1: checksum = Actuator<1>(c).run(); break;
            case 2: checksum = Actuator<2>(c).run(); break;
            case 4: checksum = Actuator<4>(c).run(); break;
            case 8: checksum = Actuator<8>(c).run(); break;
            case 16: checksum = Actuator<16>(c).run(); break;
        }
        std::cout << "PrefetchSteer mode=" << c.mode << " checksum=" << checksum << '\n';
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "PrefetchSteer: " << error.what() << '\n';
        return 1;
    }
}
