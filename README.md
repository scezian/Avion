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

## 3. Optional: run the GUI at login

Create `~/.config/autostart/fancontrol-gui.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=Fan Control
Exec=python3 /path/to/fancontrol_gui.py
X-GNOME-Autostart-enabled=true
```

(Adjust the `Exec` path to wherever you keep the script; on Hyprland you can
alternatively add an `exec-once` line to your Hyprland config instead.)

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
