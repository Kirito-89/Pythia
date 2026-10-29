#ifndef STEERING_H
#define STEERING_H
#include <cstdint>
#include <string>

// Observation and experimental controls; disabled by default.
namespace steering {
bool parse_knob(const char* name, const char* value);
void initialize();
void begin_roi();
void tick();
void finish();
void mark_complete(uint32_t cpu);
void trace_end(uint32_t cpu);
bool suppress_memory(uint32_t cpu, bool has_memory_op);
void probe_latency(uint32_t cpu, uint64_t cycles);
bool replay_is_expected(uint32_t cpu);
uint32_t oracle_bandwidth(uint32_t measured);
bool per_core_bandwidth();
bool logging();
bool stop_at_victim();
uint32_t victim_core();
bool disable_prefetch(uint32_t cpu);
bool freeze_learning(uint32_t cpu);
bool disable_hbw_reward(uint32_t cpu);
void decision(uint32_t cpu, int32_t action, uint32_t degree, uint32_t requested);
void reward(uint32_t cpu, int32_t value, int32_t type, bool high_bw);
void training(uint32_t cpu, bool applied);
void bandwidth(uint32_t bucket);
void dram_request(uint32_t cpu);
}
#endif
