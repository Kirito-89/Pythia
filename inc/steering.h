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
bool logging();
bool stop_at_victim();
uint32_t victim_core();
bool disable_prefetch(uint32_t cpu);
bool freeze_learning(uint32_t cpu);
bool disable_hbw_reward(uint32_t cpu);
// Closed-loop steering oracle: should this attacker off-chip read be
// short-circuited (attacker kept quiet) instead of loading DRAM right now?
// Reacts to the victim's most recent action. The attacker core keeps running.
bool suppress_attacker_dram(uint32_t cpu);
void decision(uint32_t cpu, int32_t action, uint32_t degree, uint32_t requested);
void reward(uint32_t cpu, int32_t value, int32_t type, bool high_bw);
void training(uint32_t cpu, bool applied);
void bandwidth(uint32_t bucket);
void dram_request(uint32_t cpu);
}
#endif
