#include "steering.h"
#include "ooo_cpu.h"
#include "uncore.h"
#include "scooby.h"
#include <array>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <sstream>
#include <vector>

// Defined in knobs.cc and populated from the config ini before initialize().
namespace knob { extern std::vector<int32_t> scooby_actions; }

namespace steering {
namespace {
std::string log_path, phase_path, cycle_phase_path;
uint64_t epoch = 10000;
uint32_t victim = 0, attacker = 1;
bool freeze = false, stop_victim = false, no_repeat = false, disable_attacker = false;
bool disable_hbw_after_warmup = false;
bool roi = false;

// Closed-loop steering oracle. The attacker core always runs and retires
// normally; the oracle modulates only its *off-chip* traffic by short-circuiting
// a fraction of its DRAM read requests (they return immediately, like the
// pre-warmup path, consuming no bandwidth). This avoids the deadlock that
// cycle-skipping causes -- a frozen core desyncs the shared DRAM timing model
// and wedges the victim -- and it survives permanent quiet, which a successful
// steer produces.
//   "conditional": binary. The attacker is QUIET (all DRAM reads suppressed)
//                  exactly while the victim's most recent action equals
//                  target_action, and LOUD otherwise. This teaches the target.
//   "budget":      action-independent. A fixed fraction budget_num/budget_den
//                  of attacker DRAM reads is suppressed every request, matched
//                  to the treatment's measured suppressed fraction. This is the
//                  budget-matched deconfounder isolating action-conditioning
//                  from raw contention volume.
bool oracle_enable = false;
std::string oracle_mode = "conditional";
int32_t target_action = 0;
bool target_action_set = false;
uint64_t budget_num = 0, budget_den = 1;   // budget-mode suppressed fraction
int32_t last_victim_action = INT32_MIN;    // sentinel: no victim decision seen yet
uint64_t oracle_requests = 0;              // attacker DRAM reads seen by the gate in ROI
uint64_t oracle_suppressed = 0;            // of those, how many were short-circuited
uint64_t budget_counter = 0;               // phases the budget duty cycle
std::ofstream output, phases_output;
struct Phase { uint64_t point; std::string name; uint64_t pulse; };
std::vector<Phase> phases;
size_t next_phase = 0;
std::string phase = "marker-overhead";
uint64_t pulse = 0, last_cycle = 0;
std::array<bool, NUM_CPUS> active{}, completed{};
struct Counters {
    uint64_t decisions = 0, zero = 0, degree = 0, predicted = 0;
    uint64_t attempts = 0, updates = 0, frozen = 0, rewards = 0, high_rewards = 0;
    int64_t reward_sum = 0;
    std::map<int32_t, uint64_t> actions;
};
std::array<Counters, NUM_CPUS> counters;
struct Snapshot {
    uint64_t retired, issued, useful, late, useless, misses, accesses;
};
std::array<Snapshot, NUM_CPUS> previous;
std::array<uint64_t, 4> bw{};
std::array<uint64_t, NUM_CPUS> requests{};
uint64_t queue_sum = 0, queue_samples = 0;

[[noreturn]] void fail(const std::string& message) {
    std::cerr << "Steering: " << message << std::endl;
    std::exit(2);
}
uint64_t integer(const std::string& text) {
    if (text.empty() || text.find_first_not_of("0123456789") != std::string::npos)
        fail("expected unsigned integer: " + text);
    try { return std::stoull(text); }
    catch (...) { fail("integer out of range: " + text); }
}
int32_t signed_integer(const std::string& text) {
    std::size_t start = (!text.empty() && (text[0] == '-' || text[0] == '+')) ? 1 : 0;
    if (text.size() == start || text.find_first_not_of("0123456789", start) != std::string::npos)
        fail("expected signed integer: " + text);
    try {
        long value = std::stol(text);
        if (value < INT32_MIN || value > INT32_MAX) fail("action out of int32 range: " + text);
        return static_cast<int32_t>(value);
    } catch (...) { fail("integer out of range: " + text); }
}
bool boolean(const std::string& text) {
    if (text == "true") return true;
    if (text == "false") return false;
    fail("expected true or false: " + text);
}
std::vector<std::string> split(const std::string& line) {
    std::vector<std::string> fields;
    std::istringstream in(line);
    std::string field;
    while (std::getline(in, field, ',')) fields.push_back(field);
    return fields;
}
std::string phase_name(uint64_t id) {
    static const char* names[] = {"pre", "pressure", "off", "post", "quiet-control"};
    if (id > 4) fail("invalid phase id");
    return names[id];
}
void read_phases() {
    if (phase_path.empty() && cycle_phase_path.empty()) return;
    if (!phase_path.empty() && !cycle_phase_path.empty()) fail("choose instruction OR cycle phases");
    const bool by_cycle = !cycle_phase_path.empty();
    std::ifstream in(by_cycle ? cycle_phase_path : phase_path);
    if (!in) fail("cannot open phase file");
    std::string line;
    if (!std::getline(in, line)) fail("empty phase file");
    if (!line.empty() && line.back() == '\r') line.pop_back();
    const auto header = split(line);
    if (by_cycle) {
        if (header.size() < 3 || header[0] != "cycle" || header[1] != "phase" || header[2] != "pulse")
            fail("expected cycle,phase,pulse header");
    } else if (line != "event,trace_instruction,phase,pulse") fail("invalid Pin phase header");
    bool begin = false, end = false;
    uint64_t last = 0;
    size_t rows = 0;
    while (std::getline(in, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.empty()) continue;
        auto fields = split(line);
        if (fields.size() < (by_cycle ? 3u : 4u)) fail("malformed phase row");
        uint64_t point = integer(fields[by_cycle ? 0 : 1]);
        if (rows && point < last) fail("out-of-order phase boundary");
        last = point;
        ++rows;
        if (by_cycle) {
            const std::string& name = fields[1];
            if (name != "marker-overhead" && name != "pre" && name != "pressure" &&
                name != "off" && name != "post" && name != "quiet-control") fail("unknown phase name");
            if (!phases.empty() && point == phases.back().point) fail("duplicate cycle phase boundary");
            phases.push_back({point, name, integer(fields[2])});
        } else {
            if (end) fail("events after ROI end");
            if (fields[0] == "roi_begin") {
                if (begin || rows != 1 || point != 0) fail("invalid ROI begin");
                begin = true;
            } else if (fields[0] == "roi_end") {
                if (!begin || phases.empty() || point <= phases.back().point) fail("invalid ROI end");
                end = true;
            } else if (fields[0] == "phase") {
                if (!begin || (!phases.empty() && point <= phases.back().point)) fail("invalid phase boundary");
                phases.push_back({point, phase_name(integer(fields[2])), integer(fields[3])});
            } else fail("incomplete or unknown Pin phase event: " + fields[0]);
        }
    }
    if (phases.empty() || (!by_cycle && (!begin || !end))) fail("expected complete phase schedule");
}
Snapshot snapshot(uint32_t cpu) {
    const CACHE& l2 = ooo_cpu[cpu].L2C;
    return {ooo_cpu[cpu].num_retired, l2.pf_issued, l2.pf_useful, l2.pf_late, l2.pf_useless,
            uncore.LLC.sim_miss[cpu][LOAD], uncore.LLC.sim_access[cpu][LOAD]};
}
uint64_t fingerprint(uint32_t cpu) {
    for (auto* p : ooo_cpu[cpu].L2C.prefetchers)
        if (p->get_type() == "scooby") return static_cast<Scooby*>(p)->policy_fingerprint();
    return 0;
}
void phase_event() {
    if (!phases_output.is_open()) return;
    phases_output << uncore.cycle << ',' << phase << ',' << pulse << ','
                  << ooo_cpu[attacker].num_retired << ',' << ooo_cpu[victim].num_retired
                  << ',' << roi << '\n';
    if (!phases_output) fail("phase log write failed");
}
void reset_interval() {
    for (uint32_t cpu = 0; cpu < NUM_CPUS; ++cpu) {
        previous[cpu] = snapshot(cpu);
        counters[cpu] = Counters();
    }
    bw.fill(0); requests.fill(0); queue_sum = queue_samples = 0;
    last_cycle = uncore.cycle;
}
void flush() {
    if (!logging() || !roi || uncore.cycle <= last_cycle) return;
    for (uint32_t cpu = 0; cpu < NUM_CPUS; ++cpu) {
        if (!active[cpu]) continue;
        const auto now = snapshot(cpu);
        const auto& old = previous[cpu];
        const auto& c = counters[cpu];
        const uint64_t instructions = now.retired - old.retired;
        output << cpu << ',' << last_cycle << ',' << uncore.cycle << ',' << phase << ',' << pulse << ','
               << old.retired << ',' << now.retired << ',' << instructions << ','
               << double(instructions) / (uncore.cycle - last_cycle) << ','
               << c.decisions << ',' << c.zero << ',' << c.degree << ',' << c.predicted << ','
               << now.issued - old.issued << ',' << now.useful - old.useful << ','
               << now.late - old.late << ',' << now.useless - old.useless << ','
               << now.misses - old.misses << ',' << now.accesses - old.accesses << ','
               << c.attempts << ',' << c.updates << ',' << c.frozen << ','
               << c.rewards << ',' << c.reward_sum << ',' << c.high_rewards;
        for (auto count : bw) output << ',' << count;
        uint64_t total_requests = 0;
        for (auto count : requests) total_requests += count;
        output << ',' << (queue_samples ? double(queue_sum) / queue_samples : 0.0)
               << ',' << total_requests << ',' << std::hex << fingerprint(cpu) << std::dec << ',';
        bool first = true;
        for (auto item : c.actions) {
            if (!first) output << ';';
            output << item.first << ':' << item.second;
            first = false;
        }
        output << ',' << requests[cpu] << '\n';
    }
    if (!output) fail("interval log write failed");
    reset_interval();
}
}

bool parse_knob(const char* key, const char* raw) {
    std::string name(key), value(raw);
    if (name.compare(0, 9, "steering_") != 0) return false;
    if (name == "steering_log") log_path = value;
    else if (name == "steering_phases") phase_path = value;
    else if (name == "steering_cycle_phases") cycle_phase_path = value;
    else if (name == "steering_epoch") epoch = integer(value);
    else if (name == "steering_victim_core") {
        auto core = integer(value); if (core >= NUM_CPUS) fail("invalid victim core"); victim = core;
    } else if (name == "steering_attacker_core") {
        auto core = integer(value); if (core >= NUM_CPUS) fail("invalid attacker core"); attacker = core;
    } else if (name == "steering_freeze_learning") freeze = boolean(value);
    else if (name == "steering_stop_at_victim") stop_victim = boolean(value);
    else if (name == "steering_no_repeat") no_repeat = boolean(value);
    else if (name == "steering_disable_attacker_prefetch") disable_attacker = boolean(value);
    else if (name == "steering_disable_hbw_reward_after_warmup") disable_hbw_after_warmup = boolean(value);
    else if (name == "steering_oracle_enable") oracle_enable = boolean(value);
    else if (name == "steering_oracle_mode") {
        if (value != "conditional" && value != "budget") fail("steering_oracle_mode must be conditional or budget");
        oracle_mode = value;
    } else if (name == "steering_oracle_target_action") { target_action = signed_integer(value); target_action_set = true; }
    else if (name == "steering_oracle_budget_num") budget_num = integer(value);
    else if (name == "steering_oracle_budget_den") { budget_den = integer(value); if (!budget_den) fail("steering_oracle_budget_den must be positive"); }
    else fail("unknown option: " + name);
    return true;
}
bool logging() { return !log_path.empty(); }
bool stop_at_victim() { return stop_victim; }
uint32_t victim_core() { return victim; }
bool disable_prefetch(uint32_t cpu) { return disable_attacker && cpu == attacker; }
bool suppress_attacker_dram(uint32_t cpu) {
    // Should this attacker off-chip read be short-circuited (quiet) instead of
    // loading DRAM? Fast path: only the attacker core, only inside the ROI.
    if (!oracle_enable || !roi || cpu != attacker) return false;
    bool suppress;
    if (oracle_mode == "conditional") {
        // Binary: quiet exactly while the victim's latest action is the target.
        suppress = (last_victim_action == target_action);
    } else {  // budget: action-independent fraction matched to the treatment
        suppress = (budget_counter % budget_den) < budget_num;
        ++budget_counter;
    }
    ++oracle_requests;
    oracle_suppressed += suppress;
    return suppress;
}
bool freeze_learning(uint32_t cpu) { return freeze && roi && cpu == victim; }
bool disable_hbw_reward(uint32_t cpu) { return disable_hbw_after_warmup && roi && cpu == victim; }
void initialize() {
    const bool configured = logging() || freeze || stop_victim || no_repeat || disable_attacker || disable_hbw_after_warmup ||
                            oracle_enable || !phase_path.empty() || !cycle_phase_path.empty();
    if (!configured) return;
    if (victim >= NUM_CPUS || attacker >= NUM_CPUS || victim == attacker) fail("requires distinct valid victim and attacker cores");
    if (!epoch) fail("steering_epoch must be positive");
    if (oracle_enable) {
        if (oracle_mode == "conditional") {
            if (!target_action_set) fail("steering_oracle_mode=conditional requires steering_oracle_target_action");
            if (!knob::scooby_actions.empty()) {
                bool found = false;
                for (auto a : knob::scooby_actions) found |= (a == target_action);
                if (!found) fail("steering_oracle_target_action is not in scooby_actions");
            }
        } else {  // budget
            if (budget_num > budget_den) fail("steering_oracle_budget_num must be <= steering_oracle_budget_den");
        }
    }
    read_phases();
    if (logging()) {
        output.open(log_path);
        phases_output.open(log_path + ".phases.csv");
        if (!output || !phases_output) fail("cannot create steering logs");
        output << std::setprecision(12);
        output << "core,cycle_begin,cycle_end,phase,pulse,retired_begin,retired_end,instructions,ipc,"
                  "decisions,zero_action,degree_sum,predicted_lines,issued_prefetches,useful_prefetches,"
                  "late_prefetches,useless_prefetches,llc_load_misses,llc_load_accesses,train_attempts,updates,"
                  "frozen_updates,rewards,reward_sum,high_bw_rewards,bw_level_0,bw_level_1,bw_level_2,bw_level_3,"
                  "dram_rq_mean,dram_requests,q_hash,action_histogram,core_dram_requests\n";
        phases_output << "cycle,phase,pulse,attacker_retired,victim_retired,roi\n";
    }
    std::cout << "steering_freeze_learning " << freeze << "\nsteering_stop_at_victim " << stop_victim
              << "\nsteering_no_repeat " << no_repeat << "\nsteering_disable_attacker_prefetch " << disable_attacker
              << "\nsteering_disable_hbw_reward_after_warmup " << disable_hbw_after_warmup
              << "\nsteering_epoch " << epoch << "\nsteering_victim_core " << victim
              << "\nsteering_attacker_core " << attacker << "\nsteering_log " << log_path
              << "\nsteering_oracle_enable " << oracle_enable << "\nsteering_oracle_mode " << oracle_mode
              << "\nsteering_oracle_target_action " << target_action
              << "\nsteering_oracle_budget_num " << budget_num << "\nsteering_oracle_budget_den " << budget_den << std::endl;
}
void begin_roi() {
    roi = true;
    if (!logging()) return;
    active.fill(true);
    reset_interval();
    for (uint32_t cpu = 0; cpu < NUM_CPUS; ++cpu)
        std::cout << "Steering_core_" << cpu << "_q_hash_at_roi " << std::hex << fingerprint(cpu) << std::dec << '\n';
}
void tick() {
    if (!logging()) return;
    if (roi) {
        for (uint32_t channel = 0; channel < DRAM_CHANNELS; ++channel) queue_sum += uncore.DRAM.RQ[channel].occupancy;
        ++queue_samples;
    }
    const uint64_t point = cycle_phase_path.empty() ? ooo_cpu[attacker].num_retired : uncore.cycle;
    while (next_phase < phases.size() && point >= phases[next_phase].point) {
        flush();
        phase = phases[next_phase].name; pulse = phases[next_phase].pulse; ++next_phase;
        phase_event();
    }
    bool ended = false;
    for (uint32_t cpu = 0; cpu < NUM_CPUS; ++cpu) ended |= completed[cpu] && active[cpu];
    if ((roi && uncore.cycle % epoch == 0) || ended) flush();
    for (uint32_t cpu = 0; cpu < NUM_CPUS; ++cpu) if (completed[cpu]) active[cpu] = false;
}
void finish() {
    flush();
    if (output.is_open()) { output.close(); if (output.fail()) fail("closing interval log failed"); }
    if (phases_output.is_open()) { phases_output.close(); if (phases_output.fail()) fail("closing phase log failed"); }
    if (oracle_enable) {
        const uint64_t loud = oracle_requests - oracle_suppressed;
        std::cout << "steering_oracle_attacker_requests " << oracle_requests
                  << "\nsteering_oracle_attacker_suppressed " << oracle_suppressed
                  << "\nsteering_oracle_attacker_loud " << loud
                  << "\nsteering_oracle_attacker_loud_fraction "
                  << (oracle_requests ? double(loud) / oracle_requests : 0.0) << std::endl;
    }
}
void mark_complete(uint32_t cpu) { completed[cpu] = true; }
void trace_end(uint32_t cpu) {
    if (no_repeat) fail("trace exhausted for core " + std::to_string(cpu) + "; replay disabled; generate a longer trace");
}
void decision(uint32_t cpu, int32_t action, uint32_t degree, uint32_t requested) {
    // Record the victim's latest action for the closed-loop oracle. This runs
    // even when interval logging is disabled, since the gate depends on it.
    if (cpu == victim) last_victim_action = action;
    if (!logging() || !roi || !active[cpu]) return;
    auto& c = counters[cpu];
    ++c.decisions; c.zero += action == 0; c.degree += action == 0 ? 0 : degree; c.predicted += requested;
    ++c.actions[action];
}
void reward(uint32_t cpu, int32_t value, int32_t, bool high_bw) {
    if (!logging() || !roi || !active[cpu]) return;
    auto& c = counters[cpu];
    ++c.rewards; c.reward_sum += value; c.high_rewards += high_bw;
}
void training(uint32_t cpu, bool applied) {
    if (!logging() || !roi || !active[cpu]) return;
    auto& c = counters[cpu];
    ++c.attempts; c.updates += applied; c.frozen += !applied;
}
void bandwidth(uint32_t bucket) { if (logging() && roi && bucket < 4) ++bw[bucket]; }
void dram_request(uint32_t cpu) { if (logging() && roi && cpu < NUM_CPUS) ++requests[cpu]; }
}
