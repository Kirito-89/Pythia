import pandas as pd
import matplotlib.pyplot as plt

# 1. Load the IPC Data
# Format: CSV_IPC, num_retired, heartbeat_ipc
df_ipc = pd.read_csv("ipc_data.csv", header=None, 
                     names=["Tag", "Instructions", "IPC"])

# 2. Load the Action/Bandwidth Data
# Format: CSV_ACTION, train_calls, bw_level, action_value
df_action = pd.read_csv("action_data.csv", header=None, 
                        names=["Tag", "Train_Calls", "BW_Level", "Action"])

# 3. Smooth the noisy data (Adjust window size if the plot is too noisy/flat)
window_size_ipc = max(1, len(df_ipc) // 50)
window_size_action = max(1, len(df_action) // 50)

df_ipc['IPC_Smoothed'] = df_ipc['IPC'].rolling(window=window_size_ipc).mean()
df_action['Action_Smoothed'] = df_action['Action'].rolling(window=window_size_action).mean()

# 4. Set up the figure with 2 subplots
fig, (ax1, ax3) = plt.subplots(2, 1, figsize=(12, 10))

# --- TOP SUBPLOT: Victim IPC ---
color_ipc = 'tab:blue'
ax1.set_title("Victim Performance (603.bwaves)", fontweight='bold')
ax1.set_xlabel('Instructions Retired', fontweight='bold')
ax1.set_ylabel('Victim IPC', color=color_ipc, fontweight='bold')
ax1.plot(df_ipc['Instructions'], df_ipc['IPC_Smoothed'], color=color_ipc, linewidth=2)
ax1.tick_params(axis='y', labelcolor=color_ipc)
ax1.grid(True, linestyle='--', alpha=0.6)

# --- BOTTOM SUBPLOT: Pythia's Action Choices & Bandwidth ---
color_action = 'tab:green'
ax3.set_title("RL Agent Behavior & System Bandwidth Contention", fontweight='bold')
ax3.set_xlabel('Training Updates (Time)', fontweight='bold')
ax3.set_ylabel('Prefetch Degree / Offset', color=color_action, fontweight='bold')
ax3.plot(df_action['Train_Calls'], df_action['Action_Smoothed'], color=color_action, linewidth=2, label="Agent Action")
ax3.tick_params(axis='y', labelcolor=color_action)

# Create a secondary y-axis for the Bandwidth Level
ax4 = ax3.twinx()
color_bw = 'tab:red'
ax4.set_ylabel('System Bandwidth Level', color=color_bw, fontweight='bold')
# Scatter plot for bandwidth to show discrete levels clearly
ax4.scatter(df_action['Train_Calls'], df_action['BW_Level'], color=color_bw, alpha=0.1, s=10, label='Raw BW Level')
ax4.tick_params(axis='y', labelcolor=color_bw)

# Highlight the newly fixed threshold
ax4.axhline(y=2, color='black', linestyle='--', linewidth=1.5, label='Poison Threshold (bw_level=2)')

# Formatting
fig.suptitle('Pythia Reward Poisoning: Successful Contention Attack', fontsize=18, fontweight='bold')
fig.tight_layout()

# Save and show
plt.savefig("reward_poisoning_attack_fixed.png", dpi=300, bbox_inches='tight')
print("Graph saved as reward_poisoning_attack_fixed.png")