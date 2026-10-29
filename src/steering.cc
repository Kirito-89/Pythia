#include "steering.h"
#include "ooo_cpu.h"
#include "uncore.h"
#include "scooby.h"
#include <algorithm>
#include <array>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <sstream>
#include <vector>

namespace steering {
namespace {
std::string log_path, phase_path, cycle_phase_path, schedule_path;
bool schedule_quiet = false;
bool per_core_bw = false;  // defense: prefetchers see only their own core's DRAM request rate
int64_t force_bw = -1;  // oracle: bandwidth bucket reported to prefetchers during pressure windows
constexpr uint64_t BIAS = 1ULL << 50;  // schedule positions are signed; stored biased
uint64_t roi_base = 0;
uint64_t gate_duty = 0;  // percent of each 1000-cycle period the attacker may use memory
uint64_t epoch = 10000;
uint32_t victim = 0, attacker = 1;
bool freeze = false, stop_victim = false, no_repeat = false, disable_attacker = false;
bool disable_hbw_after_warmup = false;
bool roi = false;
std::ofstream output, phases_output;
struct Phase { uint64_t point; std::string name; uint64_t pulse; uint64_t duty; };
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

// Closed-loop latency-probe controller. It sees only what a co-running process could measure:
// the latency of its own LLC-missing loads. It never reads the victim's Q-table, reward or
// the DRAM controller's bandwidth bucket.
bool ctl = false;
uint64_t ctl_epoch = 50000, probe_period = 0, ctl_min_samples = 16;
uint64_t ctl_calibrate = 20, ctl_settle = 10, ctl_window = 10, ctl_min_on = 30, ctl_max_on = 200;
uint64_t ctl_min_off = 10, ctl_max_off = 200, ctl_end = 0, ctl_duty = 100;
double ctl_drop = 0.05, ctl_recover = 0.03, ctl_alpha = 0.3;
enum class Ctl { Calibrate, Pressure, Relax, Track, Done };
// Tracking mode: after calibration, hold the probe-latency rise over the victim-only baseline at a
// commanded setpoint by continuously modulating attacker duty (proportional feedback). The setpoint
// profile is keyed by ROI-relative victim instructions; setpoint 0 means "stay silent".
bool ctl_track = false;
std::vector<std::pair<double, double>> ctl_targets;    // (instr count or victim-state threshold, latency-rise setpoint)
// ORACLE segment trigger: advance setpoints on the victim's own behavior, not on an instruction count.
std::string ctl_trigger = "instr";                     // instr | ipc | noprefetch
uint64_t ctl_confirm = 2, confirm_count = 0, oracle_idx = 0;
uint64_t vic_dec = 0, vic_zero = 0;                    // monotonic victim decision / no-prefetch counters
uint64_t trk_retired = 0, trk_cycle = 0, trk_dec = 0, trk_zero = 0;
double trk_ipc = 0, trk_nopf = 0;
// CYCLE mode (ORACLE): repeated down/up control by hysteresis on victim IPC. Pressure until IPC < cycle_lo,
// release until IPC > cycle_hi, repeat. Produces an oscillating victim-performance profile.
bool ctl_cycle = false, cyc_driving = false, ctl_cycle_rel = true;
double cycle_hi = 0, cycle_lo = 0, cycle_setpoint = 0;
// cycle thresholds resolved to absolute IPC at calibration (relative = fraction of measured victim-idle IPC)
double cyc_hi_abs = 0, cyc_lo_abs = 0, ipc_baseline = 0, ipc_base_sum = 0, ipc_base_n = 0;
double ctl_gain = 2.0, ctl_cur_duty = 0;               // gain: duty points per percentage point of error
uint64_t ctl_duty_min = 0, track_segment = 0;
std::ostringstream track_note;
Ctl ctl_state = Ctl::Calibrate;
uint64_t lat_sum = 0, lat_n = 0, ctl_in_state = 0, ctl_started = 0, probe_counter = 0;
double base_sum = 0, base_n = 0, baseline = 0, ref_sum = 0, ref_n = 0, reference = 0, smooth = 0, last_latency = 0;
bool smooth_init = false;
std::ofstream ctl_output;

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
double real(const std::string& text) {
    try {
        size_t used = 0;
        const double v = std::stod(text, &used);
        if (used != text.size() || v < 0) fail("expected non-negative number: " + text);
        return v;
    } catch (...) { fail("expected non-negative number: " + text); }
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
std::vector<std::string> split_on(const std::string& line, char sep) {
    std::vector<std::string> fields;
    std::istringstream in(line);
    std::string field;
    while (std::getline(in, field, sep)) fields.push_back(field);
    return fields;
}
std::string phase_name(uint64_t id) {
    static const char* names[] = {"pre", "pressure", "off", "post", "quiet-control"};
    if (id > 4) fail("invalid phase id");
    return names[id];
}
void read_phases() {
    if (!schedule_path.empty()) {
        // Victim-clocked schedule: victim_instr,phase,pulse,duty. victim_instr is signed and
        // counts retired victim instructions relative to ROI start (negative = warmup).
        if (!phase_path.empty() || !cycle_phase_path.empty()) fail("choose one phase source");
        std::ifstream in(schedule_path);
        if (!in) fail("cannot open schedule file");
        std::string line;
        if (!std::getline(in, line)) fail("empty schedule file");
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line != "victim_instr,phase,pulse,duty") fail("expected victim_instr,phase,pulse,duty header");
        while (std::getline(in, line)) {
            if (!line.empty() && line.back() == '\r') line.pop_back();
            if (line.empty()) continue;
            auto f = split(line);
            if (f.size() != 4) fail("malformed schedule row");
            const bool neg = !f[0].empty() && f[0][0] == '-';
            const uint64_t magnitude = integer(neg ? f[0].substr(1) : f[0]);
            if (magnitude >= BIAS) fail("schedule position out of range");
            const uint64_t point = neg ? BIAS - magnitude : BIAS + magnitude, duty = integer(f[3]);
            if (duty > 100) fail("duty must be 0..100");
            if (!phases.empty() && point <= phases.back().point) fail("schedule must be strictly increasing");
            if (f[1] != "pre" && f[1] != "pressure" && f[1] != "off" && f[1] != "post") fail("unknown schedule phase");
            phases.push_back({point, f[1], integer(f[2]), duty});
        }
        if (phases.empty() || phases.front().name != "pre") fail("schedule must start with a pre row at a large negative position");
        return;
    }
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
            phases.push_back({point, name, integer(fields[2]), 0});
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
                phases.push_back({point, phase_name(integer(fields[2])), integer(fields[3]), 0});
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
uint64_t schedule_position() {
    // Before the ROI the position is pinned below zero so ROI rows cannot fire early.
    const uint64_t retired = ooo_cpu[victim].num_retired;
    if (!roi) {
        const uint64_t warm = ooo_cpu[victim].warmup_instructions;
        return retired < warm ? BIAS - (warm - retired) : BIAS - 1;
    }
    return BIAS + (retired - roi_base);
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
const char* ctl_name(Ctl state) {
    return state == Ctl::Calibrate ? "calibrate" : state == Ctl::Pressure ? "pressure" : state == Ctl::Relax ? "relax"
         : state == Ctl::Track ? "track" : "done";
}
void ctl_enter(Ctl next, const char* name, uint64_t pulse_id, const char* reason) {
    flush();
    ctl_output << uncore.cycle << ",transition," << ctl_name(ctl_state) << "->" << ctl_name(next) << ','
               << pulse_id << ',' << ctl_in_state << ",,,,," << reason << '\n';
    ctl_state = next; phase = name; pulse = pulse_id; ctl_in_state = 0;
    gate_duty = next == Ctl::Pressure ? ctl_duty : 0;
    ref_sum = ref_n = 0; smooth_init = false;
    phase_event();
}
size_t target_segment() {  // instruction-clocked
    const double done = double(ooo_cpu[victim].num_retired - roi_base);
    size_t k = 0;
    while (k + 1 < ctl_targets.size() && done >= ctl_targets[k + 1].first) ++k;
    return k;
}
void track_metrics() {  // victim IPC and no-prefetch rate over the last controller epoch (ORACLE: reads victim state)
    const uint64_t r = ooo_cpu[victim].num_retired, c = uncore.cycle, d = vic_dec, z = vic_zero;
    if (c > trk_cycle) trk_ipc = double(r - trk_retired) / (c - trk_cycle);
    if (d > trk_dec) trk_nopf = double(z - trk_zero) / (d - trk_dec);
    trk_retired = r; trk_cycle = c; trk_dec = d; trk_zero = z;
}
size_t oracle_segment() {  // advance when the victim metric crosses the next segment threshold for ctl_confirm epochs
    if (oracle_idx + 1 < ctl_targets.size()) {
        const double thr = ctl_targets[oracle_idx + 1].first;
        const bool cross = ctl_trigger == "ipc" ? trk_ipc < thr : trk_nopf * 100.0 > thr;
        if (cross && ++confirm_count >= ctl_confirm) { ++oracle_idx; confirm_count = 0; }
        else if (!cross) confirm_count = 0;
    }
    return oracle_idx;
}
size_t current_segment() { return ctl_trigger == "instr" ? target_segment() : oracle_segment(); }
void track_enter_segment(size_t k) {
    flush();
    const bool on = ctl_targets[k].second > 0;
    ctl_output << uncore.cycle << ",transition,track," << k << ',' << ctl_in_state << ",,,,,"
               << "setpoint=" << ctl_targets[k].second * 100 << "%\n";
    phase = on ? "pressure" : "off"; pulse = k; track_segment = k;
    if (!on) ctl_cur_duty = 0;
    phase_event();
}
void drive_duty(double target, double smooth_now) {  // proportional duty control toward a latency-rise setpoint
    const double rise = baseline > 0 ? smooth_now / baseline - 1 : 0;
    if (target <= 0) ctl_cur_duty = 0;
    else {
        ctl_cur_duty += ctl_gain * (target - rise) * 100;
        ctl_cur_duty = std::min<double>(ctl_duty, std::max<double>(ctl_duty_min ? ctl_duty_min : 1, ctl_cur_duty));
    }
    gate_duty = static_cast<uint64_t>(ctl_cur_duty + 0.5);
}
void cycle_update(double smooth_now) {
    if (cyc_driving) {  // pressuring: release once the victim has been driven below the low IPC line
        if (trk_ipc > 0 && trk_ipc < cyc_lo_abs) { if (++confirm_count >= ctl_confirm) {
            cyc_driving = false; confirm_count = 0; ++pulse; flush(); phase = "off"; phase_event(); } }
        else confirm_count = 0;
    } else {            // released: resume pressure once the victim has recovered above the high IPC line
        if (trk_ipc > cyc_hi_abs) { if (++confirm_count >= ctl_confirm) {
            cyc_driving = true; confirm_count = 0; ++pulse; flush(); phase = "pressure"; phase_event(); } }
        else confirm_count = 0;
    }
    drive_duty(cyc_driving ? cycle_setpoint : 0, smooth_now);
    track_note.str("");
    track_note << "duty=" << gate_duty << ";driving=" << cyc_driving << ";victim_ipc=" << trk_ipc
               << ";hi=" << cyc_hi_abs << ";lo=" << cyc_lo_abs;
}
void track_update(double smooth_now) {
    if (ctl_trigger != "instr") track_metrics();
    const size_t k = current_segment();
    if (k != track_segment) track_enter_segment(k);
    const double target = ctl_targets[k].second;
    const double rise = baseline > 0 ? smooth_now / baseline - 1 : 0;
    drive_duty(target, smooth_now);
    gate_duty = static_cast<uint64_t>(ctl_cur_duty + 0.5);
    track_note.str("");
    track_note << "duty=" << gate_duty << ";setpoint=" << target * 100 << "%;rise=" << rise * 100 << '%';
    if (ctl_trigger == "ipc") track_note << ";victim_ipc=" << trk_ipc;
    else if (ctl_trigger == "noprefetch") track_note << ";victim_nopf=" << trk_nopf * 100 << '%';
}
void ctl_step() {
    const bool valid = lat_n >= ctl_min_samples;
    const uint64_t samples = lat_n;
    const double latency = valid ? double(lat_sum) / lat_n : last_latency;  // carry forward when probe is silent
    lat_sum = lat_n = 0;
    ++ctl_in_state;
    if (ctl_cycle) track_metrics();
    if (valid) {
        last_latency = latency;
        smooth = smooth_init ? ctl_alpha * latency + (1 - ctl_alpha) * smooth : latency;
        smooth_init = true;
    }
    ctl_output << uncore.cycle << ",epoch," << ctl_name(ctl_state) << ',' << pulse << ',' << ctl_in_state << ','
               << samples << ',' << latency << ',' << smooth << ',' << baseline << ',' << reference << ','
               << (ctl_state == Ctl::Track ? track_note.str() : std::string()) << "\n";
    switch (ctl_state) {
    case Ctl::Calibrate:  // attacker silent (probe only): learn the victim-only latency (and idle IPC for cycle mode)
        if (valid) { base_sum += latency; ++base_n; }
        if (ctl_cycle && trk_ipc > 0) { ipc_base_sum += trk_ipc; ++ipc_base_n; }
        if (base_n >= ctl_calibrate) {
            baseline = base_sum / base_n;
            ++ctl_started;
            if (ctl_track && ctl_cycle) {
                ipc_baseline = ipc_base_n ? ipc_base_sum / ipc_base_n : 0;
                cyc_hi_abs = ctl_cycle_rel ? cycle_hi * ipc_baseline : cycle_hi;
                cyc_lo_abs = ctl_cycle_rel ? cycle_lo * ipc_baseline : cycle_lo;
                ctl_output << uncore.cycle << ",cycle_baseline,calibrate,0,0,," << ipc_baseline
                           << ",," << cyc_hi_abs << ',' << cyc_lo_abs << ",idle_ipc\n";
                ctl_enter(Ctl::Track, "pressure", 0, "calibrated");
                cyc_driving = true; confirm_count = 0; ctl_cur_duty = ctl_duty_min ? ctl_duty_min : 1;
                trk_retired = ooo_cpu[victim].num_retired; trk_cycle = uncore.cycle; trk_dec = vic_dec; trk_zero = vic_zero;
                cycle_update(baseline);
            } else if (ctl_track) {
                // Enter segment 0 directly with its correct phase, so no placeholder phase row is
                // written at this cycle (a second same-cycle row breaks the quiet run's replay).
                const bool on0 = ctl_targets.front().second > 0;
                ctl_enter(Ctl::Track, on0 ? "pressure" : "off", 0, "calibrated");
                oracle_idx = confirm_count = 0; track_segment = 0;
                ctl_cur_duty = on0 ? (ctl_duty_min ? ctl_duty_min : 1) : 0;
                trk_retired = ooo_cpu[victim].num_retired; trk_cycle = uncore.cycle; trk_dec = vic_dec; trk_zero = vic_zero;
                track_update(baseline);
            } else
            ctl_enter(Ctl::Pressure, "pressure", ctl_started - 1, "calibrated");
        }
        break;
    case Ctl::Pressure:  // reference = latency just after onset, before the victim has adapted
        // skip the onset transient (queues filling), then average a fixed window as the reference
        if (valid && ctl_in_state > ctl_settle && ctl_in_state <= ctl_settle + ctl_window) {
            ref_sum += latency; ++ref_n; reference = ref_sum / ref_n;
        }
        if (ctl_in_state == ctl_settle + ctl_window && ref_n) { smooth = reference; smooth_init = true; }
        if (ctl_in_state > ctl_settle + ctl_window && ref_n && ctl_in_state >= ctl_min_on && smooth < reference * (1 - ctl_drop))
            ctl_enter(Ctl::Relax, "off", ctl_started - 1, "latency-dropped-victim-backed-off");
        else if (ctl_in_state >= ctl_max_on)
            ctl_enter(Ctl::Relax, "off", ctl_started - 1, "max-on");
        break;
    case Ctl::Relax:  // victim recovered when probe latency is back near the victim-only baseline
        if (smooth_init && ctl_in_state >= ctl_min_off && smooth >= baseline * (1 - ctl_recover)) {
            ++ctl_started;
            ctl_enter(Ctl::Pressure, "pressure", ctl_started - 1, "latency-recovered");
        } else if (ctl_in_state >= ctl_max_off) {
            ++ctl_started;
            ctl_enter(Ctl::Pressure, "pressure", ctl_started - 1, "max-off");
        }
        break;
    case Ctl::Track:
        if (smooth_init) { if (ctl_cycle) cycle_update(smooth); else track_update(smooth); }
        break;
    case Ctl::Done:
        break;
    }
}
bool probe_allows(bool has_memory_op) {
    if (!probe_period || !has_memory_op) return false;
    return probe_counter++ % probe_period == 0;
}
}

bool parse_knob(const char* key, const char* raw) {
    std::string name(key), value(raw);
    if (name.compare(0, 9, "steering_") != 0) return false;
    if (name == "steering_log") log_path = value;
    else if (name == "steering_phases") phase_path = value;
    else if (name == "steering_cycle_phases") cycle_phase_path = value;
    else if (name == "steering_schedule") schedule_path = value;
    else if (name == "steering_schedule_quiet") schedule_quiet = boolean(value);
    else if (name == "steering_per_core_bw") per_core_bw = boolean(value);
    else if (name == "steering_force_bw") { force_bw = integer(value); if (force_bw > 3) fail("steering_force_bw must be 0..3"); }
    else if (name == "steering_epoch") epoch = integer(value);
    else if (name == "steering_probe_controller") ctl = boolean(value);
    else if (name == "steering_probe_period") probe_period = integer(value);
    else if (name == "steering_ctl_epoch") ctl_epoch = integer(value);
    else if (name == "steering_ctl_min_samples") ctl_min_samples = integer(value);
    else if (name == "steering_ctl_calibrate") ctl_calibrate = integer(value);
    else if (name == "steering_ctl_settle") ctl_settle = integer(value);
    else if (name == "steering_ctl_window") ctl_window = integer(value);
    else if (name == "steering_ctl_min_on") ctl_min_on = integer(value);
    else if (name == "steering_ctl_max_on") ctl_max_on = integer(value);
    else if (name == "steering_ctl_min_off") ctl_min_off = integer(value);
    else if (name == "steering_ctl_max_off") ctl_max_off = integer(value);
    else if (name == "steering_ctl_end") ctl_end = integer(value);
    else if (name == "steering_ctl_duty") ctl_duty = integer(value);
    else if (name == "steering_ctl_track") ctl_track = boolean(value);
    else if (name == "steering_ctl_trigger") {
        ctl_trigger = value;
        if (ctl_trigger != "instr" && ctl_trigger != "ipc" && ctl_trigger != "noprefetch")
            fail("steering_ctl_trigger must be instr, ipc or noprefetch");
    }
    else if (name == "steering_ctl_confirm") ctl_confirm = integer(value);
    else if (name == "steering_ctl_cycle") ctl_cycle = boolean(value);
    else if (name == "steering_ctl_cycle_hi") cycle_hi = real(value);
    else if (name == "steering_ctl_cycle_lo") cycle_lo = real(value);
    else if (name == "steering_ctl_cycle_setpoint") cycle_setpoint = real(value) / 100;
    else if (name == "steering_ctl_cycle_rel") ctl_cycle_rel = boolean(value);
    else if (name == "steering_ctl_gain") ctl_gain = real(value);
    else if (name == "steering_ctl_duty_min") ctl_duty_min = integer(value);
    else if (name == "steering_ctl_targets") {  // "instr:pct,instr:pct": setpoint = latency rise over baseline, percent
        ctl_targets.clear();
        for (const auto& item : split_on(value, ',')) {
            const auto parts = split_on(item, ':');
            if (parts.size() != 2) fail("steering_ctl_targets expects instr:pct,instr:pct");
            ctl_targets.push_back({real(parts[0]), real(parts[1]) / 100});
        }
    }
    else if (name == "steering_ctl_drop_pct") ctl_drop = real(value) / 100;
    else if (name == "steering_ctl_recover_pct") ctl_recover = real(value) / 100;
    else if (name == "steering_victim_core") {
        auto core = integer(value); if (core >= NUM_CPUS) fail("invalid victim core"); victim = core;
    } else if (name == "steering_attacker_core") {
        auto core = integer(value); if (core >= NUM_CPUS) fail("invalid attacker core"); attacker = core;
    } else if (name == "steering_freeze_learning") freeze = boolean(value);
    else if (name == "steering_stop_at_victim") stop_victim = boolean(value);
    else if (name == "steering_no_repeat") no_repeat = boolean(value);
    else if (name == "steering_disable_attacker_prefetch") disable_attacker = boolean(value);
    else if (name == "steering_disable_hbw_reward_after_warmup") disable_hbw_after_warmup = boolean(value);
    else fail("unknown option: " + name);
    return true;
}
bool logging() { return !log_path.empty(); }
bool stop_at_victim() { return stop_victim; }
uint32_t victim_core() { return victim; }
bool disable_prefetch(uint32_t cpu) { return disable_attacker && cpu == attacker; }
bool freeze_learning(uint32_t cpu) { return freeze && roi && cpu == victim; }
bool disable_hbw_reward(uint32_t cpu) { return disable_hbw_after_warmup && roi && cpu == victim; }
void initialize() {
    const bool configured = logging() || freeze || stop_victim || no_repeat || disable_attacker || disable_hbw_after_warmup ||
                            !phase_path.empty() || !cycle_phase_path.empty() || !schedule_path.empty() || schedule_quiet || per_core_bw || ctl;
    if (!configured) return;
    if (victim >= NUM_CPUS || attacker >= NUM_CPUS || victim == attacker) fail("requires distinct valid victim and attacker cores");
    if (!epoch) fail("steering_epoch must be positive");
    if (ctl) {
        if (!logging()) fail("probe controller requires steering_log");
        if (!schedule_path.empty() || schedule_quiet || !phase_path.empty() || !cycle_phase_path.empty())
            fail("probe controller replaces schedule and phase files");
        if (!ctl_window) fail("steering_ctl_window must be positive");
        if (ctl_track && ctl_cycle) {
            if (!(cycle_hi > cycle_lo && cycle_lo > 0)) fail("steering_ctl_cycle needs cycle_hi > cycle_lo > 0");
            if (ctl_cycle_rel && cycle_hi > 2) fail("relative steering_ctl_cycle_hi should be a fraction of idle IPC (<=2)");
            if (cycle_setpoint <= 0) fail("steering_ctl_cycle_setpoint must be positive");
            if (!ctl_confirm) fail("steering_ctl_confirm must be positive");
            if (ctl_duty_min > ctl_duty) fail("steering_ctl_duty_min exceeds steering_ctl_duty");
        } else if (ctl_track) {
            if (ctl_targets.empty()) fail("steering_ctl_track needs steering_ctl_targets");
            if (ctl_duty_min > ctl_duty) fail("steering_ctl_duty_min exceeds steering_ctl_duty");
            if (!ctl_confirm) fail("steering_ctl_confirm must be positive");
            if (ctl_trigger == "instr") {
                if (ctl_targets.front().first != 0) fail("instr trigger needs steering_ctl_targets starting at 0");
                for (size_t i = 1; i < ctl_targets.size(); ++i)
                    if (ctl_targets[i].first <= ctl_targets[i-1].first) fail("instr-trigger targets must strictly increase");
            }
        }
        if (!ctl_epoch || !probe_period || !ctl_calibrate || ctl_duty > 100 || !ctl_duty)
            fail("probe controller needs positive epoch, probe period, calibration epochs and duty 1..100");
        if (ctl_drop >= 1 || ctl_recover >= 1) fail("drop and recover percentages must be below 100");
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
        if (ctl) {
            ctl_output.open(log_path + ".controller.csv");
            if (!ctl_output) fail("cannot create controller log");
            ctl_output << std::setprecision(8)
                       << "cycle,kind,state,pulse,epochs_in_state,probe_samples,probe_latency,smoothed_latency,"
                          "baseline_latency,reference_latency,reason\n";
        }
    }
    std::cout << "steering_freeze_learning " << freeze << "\nsteering_stop_at_victim " << stop_victim
              << "\nsteering_no_repeat " << no_repeat << "\nsteering_disable_attacker_prefetch " << disable_attacker
              << "\nsteering_disable_hbw_reward_after_warmup " << disable_hbw_after_warmup
              << "\nsteering_epoch " << epoch << "\nsteering_victim_core " << victim
              << "\nsteering_attacker_core " << attacker << "\nsteering_log " << log_path << "\nsteering_force_bw_ORACLE " << force_bw << "\nsteering_per_core_bw_DEFENSE " << per_core_bw << std::endl;
}
void begin_roi() {
    roi = true;
    roi_base = ooo_cpu[victim].num_retired;
    if (ctl) { phase = "pre"; pulse = 0; gate_duty = 0; lat_sum = lat_n = 0; }
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
    const uint64_t point = !schedule_path.empty() ? schedule_position()
                         : cycle_phase_path.empty() ? ooo_cpu[attacker].num_retired : uncore.cycle;
    while (next_phase < phases.size() && point >= phases[next_phase].point) {
        flush();
        phase = phases[next_phase].name; pulse = phases[next_phase].pulse;
        gate_duty = (phase == "pressure") ? phases[next_phase].duty : 0; ++next_phase;
        phase_event();
    }
    if (ctl && roi && ctl_state != Ctl::Done) {
        if (ctl_end && ooo_cpu[victim].num_retired - roi_base >= ctl_end) ctl_enter(Ctl::Done, "post", ctl_started, "end-of-attack");
        else if (uncore.cycle % ctl_epoch == 0) ctl_step();
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
    if (ctl_output.is_open()) { ctl_output.close(); if (ctl_output.fail()) fail("closing controller log failed"); }
}
void mark_complete(uint32_t cpu) { completed[cpu] = true; }
uint32_t oracle_bandwidth(uint32_t measured) {
    // ORACLE (upper bound, not an application): replace the measured bucket in pressure windows.
    if (force_bw < 0 || schedule_path.empty() || phase != "pressure" || !gate_duty) return measured;
    return static_cast<uint32_t>(force_bw);
}
bool per_core_bandwidth() { return per_core_bw; }
bool replay_is_expected(uint32_t cpu) { return cpu == attacker && (!schedule_path.empty() || schedule_quiet || ctl); }
bool suppress_memory(uint32_t cpu, bool has_memory_op) {
    if (cpu != attacker || (schedule_path.empty() && !schedule_quiet && !ctl)) return false;
    if (!schedule_quiet && gate_duty) return (uncore.cycle % 1000) >= gate_duty * 10;
    return !probe_allows(has_memory_op);  // closed gate: only the sparse latency probe gets through
}
void probe_latency(uint32_t cpu, uint64_t cycles) {
    if (ctl && roi && cpu == attacker) { lat_sum += cycles; ++lat_n; }
}
void trace_end(uint32_t cpu) {
    if (replay_is_expected(cpu)) return;  // periodic pressure trace: replay is part of the design
    if (no_repeat) fail("trace exhausted for core " + std::to_string(cpu) + "; replay disabled; generate a longer trace");
}
void decision(uint32_t cpu, int32_t action, uint32_t degree, uint32_t requested) {
    if (roi && cpu == victim) { ++vic_dec; vic_zero += (action == 0); }
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
