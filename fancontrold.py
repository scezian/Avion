#!/usr/bin/env python3
"""
fancontrold - ThinkPad temperature-based fan control daemon

Reads CPU package temp, drives /proc/acpi/ibm/fan with a hysteresis
curve, ramps ahead of fast temp rises, applies a fan-level floor for
known heavy apps, logs spike attribution, and (optionally) renices
configured background processes when hot. Falls back safely to 'auto'
if it can't keep up, thanks to the ACPI watchdog.
"""

import os
import re
import sys
import time
import json
import signal
import logging
import logging.handlers
import subprocess
from pathlib import Path
from collections import deque

try:
    import tomllib  # py3.11+
except ImportError:
    import tomli as tomllib  # fallback if on older python

CONFIG_PATH = Path("/etc/fancontrold/config.toml")
MODE_FILE = Path("/run/fancontrold/mode")      # "auto" or "manual"
LEVEL_FILE = Path("/run/fancontrold/level")    # manual level (0-7, "auto", "full-speed", "disengaged")
FAN_PROC = Path("/proc/acpi/ibm/fan")
TEMP_PATH = Path("/sys/class/hwmon")  # we'll search for coretemp package

LOG = logging.getLogger("fancontrold")

DEFAULT_CONFIG = {
    "poll_interval": 3,
    "watchdog_timeout": 30,
    "hysteresis": 3,
    # (temp_threshold_C, fan_level) - must be ascending by temp
    "curve": [
        [0, 0],
        [50, 1],
        [58, 2],
        [65, 3],
        [72, 4],
        [78, 5],
        [85, 6],
        [92, 7],
    ],
    "throttle_temp": 75,       # start renicing configured processes above this
    "throttle_recover_temp": 68,  # stop renicing once back below this
    "throttle_nice": 15,
    "throttle_processes": [],  # e.g. ["chromium", "firefox"]

    # rate-of-change ramping: if temp rises faster than this, bump the
    # fan ahead of what the static curve alone would give it
    "ramp_rate_window": 10,      # seconds
    "ramp_rate_threshold": 3.0,  # degrees C risen within ramp_rate_window
    "ramp_boost_levels": 2,      # levels to add above the curve's answer when ramping

    # app-aware profiles: force a minimum fan level whenever a listed
    # process is running, so the fan gets ahead of known-heavy apps
    # instead of only reacting once they've already made you hot
    "app_profiles": [
        # {"process": "chromium", "min_level": 3},
    ],

    # spike attribution logging: whenever the fan level jumps by at
    # least this much in one poll, snapshot top CPU processes and log
    # them, so you can see what's actually causing hot spikes over time
    "spike_log_enabled": True,
    "spike_log_path": "/var/log/fancontrold/spikes.jsonl",
    "spike_level_jump": 2,

    # regular daemon activity log (separate from the spike attribution log
    # above) - rotates daily, keeping log_retention_days worth of history
    "log_path": "/var/log/fancontrold/fancontrold.log",
    "log_retention_days": 7,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            user_cfg = tomllib.load(f)
        cfg.update(user_cfg)
    return cfg


def find_package_temp_input():
    """Find the hwmon input file for the coretemp 'Package id 0' sensor."""
    for hwmon in TEMP_PATH.glob("hwmon*"):
        name_file = hwmon / "name"
        if not name_file.exists():
            continue
        if name_file.read_text().strip() != "coretemp":
            continue
        for label_file in hwmon.glob("temp*_label"):
            if "Package" in label_file.read_text():
                idx = re.search(r"temp(\d+)_label", label_file.name).group(1)
                return hwmon / f"temp{idx}_input"
    raise RuntimeError("Could not find coretemp package sensor")


def read_temp(temp_input_path):
    raw = temp_input_path.read_text().strip()
    return int(raw) / 1000.0


def set_watchdog(timeout):
    FAN_PROC.write_text(f"watchdog {timeout}\n")


def set_fan_level(level):
    """level: int 0-7, or 'auto', 'full-speed', 'disengaged'"""
    FAN_PROC.write_text(f"level {level}\n")


def read_mode():
    if MODE_FILE.exists():
        m = MODE_FILE.read_text().strip()
        if m in ("auto", "manual"):
            return m
    return "auto"


def read_manual_level():
    if LEVEL_FILE.exists():
        return LEVEL_FILE.read_text().strip()
    return "auto"


def level_for_temp(curve, temp, current_level, hysteresis):
    """Ascending curve, with hysteresis to prevent flapping at boundaries."""
    target = curve[0][1]
    for threshold, level in curve:
        if temp >= threshold:
            target = level
    # Hysteresis: only drop a level if we're comfortably below the
    # threshold that would have gotten us to current_level.
    if target < current_level:
        for threshold, level in curve:
            if level == current_level:
                if temp > threshold - hysteresis:
                    return current_level
    return target


def compute_rate(history, window_seconds):
    """Degrees C risen per second, looking back up to window_seconds."""
    if len(history) < 2:
        return 0.0
    now_t, now_temp = history[-1]
    old_t, old_temp = history[0]
    for t, temp_v in history:
        if now_t - t <= window_seconds:
            old_t, old_temp = t, temp_v
            break
    dt = now_t - old_t
    if dt <= 0:
        return 0.0
    return (now_temp - old_temp) / dt


def process_running(name):
    try:
        pids = subprocess.run(
            ["pgrep", "-x", name], capture_output=True, text=True, timeout=2
        ).stdout.split()
        return len(pids) > 0
    except Exception:
        return False


def app_profile_floor(cfg):
    """Highest min_level among configured app_profiles whose process is running."""
    floor = 0
    for prof in cfg.get("app_profiles") or []:
        proc = prof.get("process")
        min_level = prof.get("min_level", 0)
        if proc and process_running(proc):
            floor = max(floor, min_level)
    return floor


def get_top_processes_snapshot(limit=5):
    """Lightweight top-CPU snapshot via ps, skipping kernel threads."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "comm:40,pcpu", "--sort=-pcpu", "--no-headers"],
            capture_output=True, text=True, timeout=2,
        ).stdout
        results = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.rsplit(None, 1)
            if len(parts) != 2:
                continue
            name, cpu = parts
            if name.startswith("[") or name in ("ps", "kworker"):
                continue
            try:
                cpu_f = float(cpu)
            except ValueError:
                continue
            results.append({"name": name, "cpu": cpu_f})
            if len(results) >= limit:
                break
        return results
    except Exception:
        return []


def relax_dir_permissions(path):
    """Let the 'fancontrol' group manage a directory the daemon creates as
    root - so log folders (which may live outside /var/log, e.g. inside a
    project folder) aren't locked to root-only for a user who just wants to
    read/delete/move their own logs without sudo."""
    try:
        import grp
        gid = grp.getgrnam("fancontrol").gr_gid
        os.chown(path, -1, gid)
        os.chmod(path, 0o775)
    except Exception:
        pass  # fancontrol group not set up yet - falls back to root-owned, still readable via 0644 file perms


def log_spike(cfg, temp, level, rate):
    if not cfg.get("spike_log_enabled", True):
        return
    path = Path(cfg.get("spike_log_path", "/var/log/fancontrold/spikes.jsonl"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        relax_dir_permissions(path.parent)
        entry = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "temp": round(temp, 1),
            "level": level,
            "rate_c_per_min": round(rate * 60, 1) if rate else 0.0,
            "top_processes": get_top_processes_snapshot(5),
        }
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        os.chmod(path, 0o644)  # let the (non-root) GUI read it too
    except Exception as e:
        LOG.warning("spike logging failed: %s", e)


def apply_throttling(cfg, hot):
    procs = cfg.get("throttle_processes") or []
    if not procs:
        return
    nice_val = str(cfg["throttle_nice"]) if hot else "0"
    for pname in procs:
        try:
            pids = subprocess.run(
                ["pgrep", "-x", pname], capture_output=True, text=True
            ).stdout.split()
            for pid in pids:
                subprocess.run(["renice", "-n", nice_val, "-p", pid],
                                capture_output=True)
        except Exception as e:
            LOG.warning("throttle failed for %s: %s", pname, e)


def setup_logging(cfg):
    log_path = Path(cfg.get("log_path", "/var/log/fancontrold/fancontrold.log"))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        relax_dir_permissions(log_path.parent)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            log_path,
            when="midnight",
            interval=1,
            backupCount=cfg.get("log_retention_days", 7),
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
        os.chmod(log_path, 0o644)  # readable without root
    except Exception as e:
        LOG.warning("could not set up file logging at %s: %s", log_path, e)


def main():
    cfg = load_config()
    setup_logging(cfg)
    MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(MODE_FILE.parent, 0o777)  # directory perms alone don't cover existing files
    if not MODE_FILE.exists():
        MODE_FILE.write_text("auto")
    if not LEVEL_FILE.exists():
        LEVEL_FILE.write_text("auto")
    os.chmod(MODE_FILE, 0o666)
    os.chmod(LEVEL_FILE, 0o666)

    # Let the fancontrol group read/write these without sudo, so the
    # dashboard GUI (running as a normal user) can flip modes directly.
    try:
        import grp
        gid = grp.getgrnam("fancontrol").gr_gid
        os.chown(MODE_FILE.parent, -1, gid)
        os.chmod(MODE_FILE.parent, 0o775)
        for f in (MODE_FILE, LEVEL_FILE):
            os.chown(f, -1, gid)
            os.chmod(f, 0o664)
    except KeyError:
        LOG.warning("fancontrol group not found - GUI will need sudo to "
                    "change modes. Run: sudo groupadd fancontrol && "
                    "sudo usermod -aG fancontrol $USER")

    temp_input = find_package_temp_input()
    LOG.info("Using temp sensor: %s", temp_input)

    set_watchdog(cfg["watchdog_timeout"])
    LOG.info("Watchdog armed at %ss - fan reverts to auto if we stop feeding it",
              cfg["watchdog_timeout"])

    current_level = 0
    is_hot = False
    temp_history = deque(maxlen=60)

    def handle_exit(signum, frame):
        LOG.info("Exiting, handing control back to auto")
        try:
            set_fan_level("auto")
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_exit)
    signal.signal(signal.SIGINT, handle_exit)

    while True:
        try:
            temp = read_temp(temp_input)
            mode = read_mode()
            temp_history.append((time.monotonic(), temp))

            if mode == "manual":
                lvl = read_manual_level()
                set_fan_level(lvl)
            else:
                base_level = level_for_temp(
                    cfg["curve"], temp, current_level, cfg["hysteresis"]
                )

                rate = compute_rate(temp_history, cfg["ramp_rate_window"])
                projected_rise = rate * cfg["ramp_rate_window"]
                ramping = projected_rise >= cfg["ramp_rate_threshold"]
                if ramping:
                    base_level = min(7, base_level + cfg["ramp_boost_levels"])
                    LOG.info("Ramping ahead: temp rising %.1fC/%ss, boosting to level %s",
                              projected_rise, cfg["ramp_rate_window"], base_level)

                floor = app_profile_floor(cfg)
                target_level = max(base_level, floor)

                if target_level - current_level >= cfg.get("spike_level_jump", 2):
                    log_spike(cfg, temp, target_level, rate)

                current_level = target_level
                set_fan_level(current_level)

            # process throttling only applies in auto mode
            if mode == "auto":
                if not is_hot and temp >= cfg["throttle_temp"]:
                    is_hot = True
                    LOG.info("Temp %.1fC crossed throttle threshold, "
                             "reprioritizing configured processes", temp)
                    apply_throttling(cfg, hot=True)
                elif is_hot and temp <= cfg["throttle_recover_temp"]:
                    is_hot = False
                    LOG.info("Temp %.1fC recovered, restoring process priority", temp)
                    apply_throttling(cfg, hot=False)

            # re-arm watchdog each loop so it never fires while we're alive
            set_watchdog(cfg["watchdog_timeout"])

            LOG.debug("temp=%.1fC mode=%s level=%s hot=%s",
                       temp, mode, current_level, is_hot)

        except Exception as e:
            LOG.error("loop error: %s", e)

        time.sleep(cfg["poll_interval"])


if __name__ == "__main__":
    if os.geteuid() != 0:
        print("fancontrold must run as root (needs to write /proc/acpi/ibm/fan)",
              file=sys.stderr)
        sys.exit(1)
    main()
