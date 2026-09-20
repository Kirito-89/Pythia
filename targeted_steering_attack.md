# Targeted Steering Reward-Poisoning Attack

## Goal

Implement a targeted feedback loop against Pythia's RL prefetcher:

- Core 0 is the victim running Pythia.
- Core 1 is the attacker running a memory-intensive trace.
- When Pythia selects the target offset, for example `+10`, Core 1 becomes quiet.
- When Pythia selects any other offset, Core 1 runs normally and increases DRAM pressure.

The intended effect is:

```text
Victim action == +10  -> attacker quiet -> lower bandwidth -> normal reward
Victim action != +10  -> attacker active -> higher bandwidth -> high-BW penalty
```

This is an experimental simulator-level controller. It is useful for validating causal steering, but it is not yet a fully external black-box attacker because the simulator directly observes Pythia's action.

## Recommended control point

Put the attacker gate in the multicore scheduling loop in `src/main.cc`, before Core 1's pipeline work:

```cpp
for (int index = 0; index < NUM_CPUS; ++index) {
    int i = get_next_cpu();

    if (i == ATTACKER_CORE && should_stall_attacker()) {
        continue;
    }

    current_core_cycle[i]++;

    // fetch, execute, memory, and retire work...
}
```

This is preferable to modifying the issue queue, DRAM scheduler, or `fetch_stall`:

- The issue queue may already contain attacker instructions.
- The DRAM controller changes memory service rather than attacker behavior.
- `fetch_stall` is already used for branch-misprediction recovery.
- Skipping the attacker's scheduling turn prevents it from fetching, issuing, executing, and retiring new instructions.
- Existing attacker memory requests can still drain from the memory system.

Put the gate before `current_core_cycle[i]++` so that the attacker core's local progress is frozen while the rest of the simulator continues.

## 1. Publish the latest Scooby action

The action should be captured immediately after Scooby's learning engine selects it. Do not extract it from `train()` or `reward()`, because those functions process delayed prefetch-tracker entries and may refer to an older action.

### Add a field in `inc/scooby.h`

```cpp
private:
    int32_t last_action;

public:
    int32_t get_last_action() const;
```

### Initialize it in the Scooby constructor

In `prefetcher/scooby.cc`:

```cpp
last_action = 0;
```

### Update it in `Scooby::predict()`

Immediately after the learning engine chooses the action:

```cpp
if (knob::scooby_enable_featurewise_engine)
{
    float max_to_avg_q_ratio = 1.0;
    action_index = brain_featurewise->chooseAction(
        state, max_to_avg_q_ratio, consensus_vec);

    last_action = Actions[action_index];

    if (knob::scooby_enable_dyn_degree)
    {
        pref_degree = get_dyn_pref_degree(
            max_to_avg_q_ratio, page, Actions[action_index]);
    }

    if (knob::scooby_enable_state_action_stats)
    {
        update_stats(state, action_index, pref_degree);
    }
}
else
{
    uint32_t state_index = state->value();
    assert(state_index < knob::scooby_max_states);

    action_index = brain->chooseAction(state_index);
    last_action = Actions[action_index];

    if (knob::scooby_enable_state_action_stats)
    {
        update_stats(state_index, action_index, pref_degree);
    }
}
```

The accessor implementation is:

```cpp
int32_t Scooby::get_last_action() const
{
    return last_action;
}
```

A simpler alternative is to set `last_action` once after the common action-selection branches:

```cpp
assert(action_index < knob::scooby_max_actions);
last_action = Actions[action_index];
```

This is preferable because it avoids duplicating the assignment in both branches.

## 2. Read Scooby's action from Core 0

Add this include to `src/main.cc`:

```cpp
#include "scooby.h"
```

Define the core and target constants:

```cpp
constexpr int VICTIM_CORE = 0;
constexpr int ATTACKER_CORE = 1;
constexpr int TARGET_ACTION = 10;
```

Since `CACHE::prefetchers` is public, the minimal accessor searches Core 0's L2 prefetchers:

```cpp
int32_t get_victim_action()
{
    for (auto* prefetcher : ooo_cpu[VICTIM_CORE].L2C.prefetchers)
    {
        auto* scooby = dynamic_cast<Scooby*>(prefetcher);
        if (scooby != nullptr)
            return scooby->get_last_action();
    }

    // No Scooby instance found, or no action has been selected yet.
    return 0;
}
```

Searching the vector is safer than assuming Scooby is always at index zero.

## 3. Gate the attacker

The simplest targeted controller is:

```cpp
bool should_stall_attacker()
{
    const int32_t victim_action = get_victim_action();

    // Keep the attacker quiet when the target action is selected.
    return victim_action == TARGET_ACTION;
}
```

Use it in the scheduler:

```cpp
for (int index = 0; index < NUM_CPUS; ++index) {
    int i = get_next_cpu();

    if (i == ATTACKER_CORE && should_stall_attacker()) {
        continue;
    }

    current_core_cycle[i]++;

    if (stall_cycle[i] <= current_core_cycle[i]) {
        if (ooo_cpu[i].ROB.occupancy < ooo_cpu[i].ROB.SIZE) {
            if (ooo_cpu[i].fetch_stall == 0)
                ooo_cpu[i].handle_branch();
        }

        ooo_cpu[i].fetch_instruction();
        ooo_cpu[i].schedule_instruction();
        ooo_cpu[i].execute_instruction();
        ooo_cpu[i].schedule_memory_instruction();
        ooo_cpu[i].execute_memory_instruction();
        ooo_cpu[i].update_rob();

        if ((ooo_cpu[i].ROB.entry[ooo_cpu[i].ROB.head].executed == COMPLETED) &&
            (ooo_cpu[i].ROB.entry[ooo_cpu[i].ROB.head].event_cycle <=
             current_core_cycle[i])) {
            ooo_cpu[i].retire_rob();
        }
    }
}
```

## 4. More realistic quiet periods

A one-cycle on/off decision can produce unstable bandwidth behavior. A quiet interval adds hysteresis:

```cpp
uint64_t attacker_quiet_until = 0;

bool should_stall_attacker()
{
    const int32_t victim_action = get_victim_action();

    if (victim_action == TARGET_ACTION) {
        attacker_quiet_until = uncore.cycle + 2000;
        return true;
    }

    return uncore.cycle < attacker_quiet_until;
}
```

Be aware that this implementation extends the quiet interval every time the target action is observed. If that is not desired, use an edge-triggered update:

```cpp
int32_t previous_action = 0;
uint64_t attacker_quiet_until = 0;

bool should_stall_attacker()
{
    const int32_t current_action = get_victim_action();

    if (current_action == TARGET_ACTION && previous_action != TARGET_ACTION)
        attacker_quiet_until = uncore.cycle + 2000;

    previous_action = current_action;
    return uncore.cycle < attacker_quiet_until;
}
```

## 5. Timing considerations

The scheduler may randomize core order through:

```cpp
int i = get_next_cpu();
```

Therefore, Core 1 may be scheduled before Core 0 selects a new action during the same global iteration. The attacker normally reacts at the next scheduling opportunity.

This delay is realistic and should be retained in the experiment. If exact event ordering is required, publish the action during Core 0's prefetch operation and consume the previously published action when Core 1 is scheduled.

The action is selected inside:

```text
Scooby::invoke_prefetcher()
    -> Scooby::predict()
        -> brain or brain_featurewise chooseAction()
```

It is not selected during reward training. The reward is delayed and may correspond to an older prefetch tracker entry.

## 6. Logging and validation

Add a temporary log at the attacker control point:

```cpp
if (i == ATTACKER_CORE) {
    std::cout << "STEERING,"
              << uncore.cycle << ","
              << get_victim_action() << ","
              << (should_stall_attacker() ? 1 : 0)
              << std::endl;
}
```

Validate the attack using all of these signals:

```text
scooby_predict_action_10
DRAM_bw_level_2
DRAM_bw_level_3
DEBUG_POISON: High-BW reward table triggered!
```

The essential checks are:

1. Core 0 actually selects action `+10` at some point.
2. Core 1 is stalled during those periods.
3. Bandwidth falls after the attacker is stalled.
4. Core 1 resumes when another action is selected.
5. High-bandwidth reward updates occur for non-target actions.
6. Victim performance and action distributions differ from a no-attacker control.

Do not infer poisoning only from an action plot. Confirm the high-bandwidth reward path directly.

## 7. Configuration requirements

The bandwidth threshold must be reachable. ChampSim quantizes bandwidth into levels `0`, `1`, `2`, and `3`. A threshold of `4` can never trigger the high-bandwidth branch.

Use a threshold such as:

```ini
scooby_high_bw_thresh = 2
```

The high-bandwidth path must also be enabled:

```ini
scooby_enable_hbw_reward = true
```

The high-bandwidth reward changes the reward table; it does not guarantee that the learned action immediately becomes zero. The expected behavior is gradual policy adaptation toward actions that perform better under the poisoned reward distribution.

## 8. Threat-model limitation

Directly reading Core 0's action inside `main.cc` gives the simulator controller privileged information. This is appropriate for an initial causal experiment, but it is not a pure external black-box attack.

A later realism stage should replace the direct action read with an observable side channel, such as:

- victim timing or IPC observations;
- shared bandwidth observations;
- cache or memory traffic measurements;
- an attacker policy that only observes its own progress and resource feedback.

The current implementation should therefore be described as a simulator-level targeted steering oracle used to test whether action-conditional resource contention can poison Pythia's online learning.
