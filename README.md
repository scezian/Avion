# ThinkPad Fan Control

Files:
- `fancontrold.py` — root daemon: temp-based fan curve + optional process throttling
- `fanctl.py` — CLI for manual override
- `fancontrol_gui.py` — PySide6 visual dashboard (live temp graph, fan curve chart, manual controls, tray icon)
- `fancontrold.service` — systemd unit for the daemon
- `config.toml` — daemon config (fan curve, throttling, timings)

## 1. Install backend (daemon + CLI)

```bash
sudo mkdir -p /etc/fancontrold /run/fancontrold
sudo cp config.toml /etc/fancontrold/config.toml
sudo cp fancontrold.py /usr/local/bin/fancontrold.py
sudo cp fanctl.py /usr/local/bin/fanctl
sudo chmod +x /usr/local/bin/fancontrold.py /usr/local/bin/fanctl
sudo cp fancontrold.service /etc/systemd/system/fancontrold.service

# python3.11+ ships tomllib built-in; on older python:
# sudo pip install tomli --break-system-packages

sudo systemctl daemon-reload
sudo systemctl enable --now fancontrold
sudo journalctl -u fancontrold -f
```

Confirm it's working:

```bash
sudo fanctl status
```

## 2. Install the GUI

```bash
pip install PySide6 psutil --break-system-packages
python3 fancontrol_gui.py
```

`psutil` powers the process picker in the Throttled Processes section —
clicking "Add" samples live CPU usage and lets you pick from the top
consumers instead of typing a name blind.

The GUI reads temps directly from sysfs and `/proc/acpi/ibm/fan`, and writes
manual overrides to `/run/fancontrold/mode` / `level` — the daemon picks these
up on its next poll (every 3s by default). No sudo needed to run the GUI
itself, since the service's runtime directory is world-writable
(`RuntimeDirectoryMode=0777`) — acceptable tradeoff on a personal single-user
laptop; tighten with a dedicated group if you want it stricter.

It minimizes to a tray icon on close (colored by current temp) with a
right-click menu for quick level changes.

## 3. Autostart the GUI at boot (systemd --user service)

`setup.sh` handles this automatically when you choose to install the GUI, but
here's what it does and how to verify or redo it manually:

**What gets installed:**
- `~/.config/systemd/user/fancontrol-gui.service` — a systemd user service
  that runs `fancontrol_gui.py`, restarting it if it crashes.
- One line added to `~/.config/hypr/hyprland.conf`:
  ```
  exec-once = systemctl --user import-environment WAYLAND_DISPLAY XDG_CURRENT_DESKTOP DISPLAY && systemctl --user start fancontrol-gui.service
  ```
  This is required, not optional — systemd user services don't automatically
  have access to your graphical session's environment (which Wayland display
  to draw to, etc.) unless something explicitly imports it. Hyprland doesn't
  do this on its own, so this line does it and then starts the service, every
  time Hyprland starts.
- An `avion` alias in your `~/.zshrc` (or `~/.bashrc`):
  ```bash
  alias avion='systemctl --user restart fancontrol-gui.service'
  ```
  Run `avion` any time to relaunch the dashboard — useful after quitting it
  from the tray menu, since that stops the service rather than the whole
  service definition.

**Verifying it actually works** (do this once, after a reboot):

```bash
systemctl --user status fancontrol-gui.service
```

Should show `active (running)`. If it shows `inactive` or `failed`, the
Hyprland env-import line either isn't in your config or didn't run — check:

```bash
grep -A1 "fancontrol-gui" ~/.config/hypr/hyprland.conf
```

and confirm that line is present and not commented out, then try `avion`
manually to see if it starts on demand (which tells you the service file
itself is fine, and it's just the boot-time env import that needs fixing).

**Manual install** (if you skipped it during `setup.sh` or want to redo it):

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/fancontrol-gui.service <<EOF
[Unit]
Description=Fan Control GUI dashboard
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=$(command -v python3) /path/to/fancontrol_gui.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now fancontrol-gui.service
```

Then add the `exec-once` line above to your `hyprland.conf` and the `avion`
alias to your shell rc file yourself.

## Notes

- `throttle_processes` in `config.toml` is what actually gets reniced when hot.
  Add process names (as `pgrep -x` would match them), e.g. `["chromium", "code"]`.
  The GUI's process list editor is a live view for this session — copy your
  final list into `config.toml` and restart the daemon to persist it.
- The ACPI watchdog (`watchdog_timeout` in config) means if the daemon crashes
  or is killed, the fan automatically reverts to `auto` after that many seconds
  — it can never get stuck at a low/off level.
- Fan curve uses hysteresis so it won't rapidly flap between levels at a
  threshold boundary.

### Rate-of-change ramping

If temp rises `ramp_rate_threshold` degrees or more within `ramp_rate_window`
seconds, the fan jumps `ramp_boost_levels` above what the static curve alone
would give it — gets ahead of a fast spike instead of reacting one poll late.

### App-aware profiles

`app_profiles` forces a minimum fan level whenever a listed process is
running, regardless of current temp:

```toml
app_profiles = [
  { process = "chromium", min_level = 3 },
]
```

Use the spike log below to figure out which apps are actually worth adding.

### Spike attribution logging

Whenever the fan level jumps by `spike_level_jump` or more in a single poll,
the daemon snapshots the top 5 CPU-consuming processes and appends them as a
JSON line to `spike_log_path` (default `/var/log/fancontrold/spikes.jsonl`):

```bash
tail -f /var/log/fancontrold/spikes.jsonl | jq .
```

World-readable (0644), so no root needed to inspect it.

### Regular activity log

Separate from the spike log above, the daemon's normal activity (temp/level
per poll at debug level, throttle events, ramping events, startup info) is
kept as a rotating plain-text file, not just in `journalctl`:

```bash
tail -f /var/log/fancontrold/fancontrold.log
```

Rotates daily at midnight, keeping `log_retention_days` (default 7) worth of
history. World-readable, same as the spike log.
