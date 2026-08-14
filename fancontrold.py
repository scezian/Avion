#!/usr/bin/env python3
"""
fancontrold - ThinkPad temperature-based fan control daemon

Reads CPU package temp, drives /proc/acpi/ibm/fan with a hysteresis
curve, and (optionally) renices configured background processes when
hot. Falls back safely to 'auto' if it can't keep up, thanks to the
ACPI watchdog.
"""

import os
import re
import sys
import time
import signal
import logging
import subprocess
from pathlib import Path

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
    "throttle_temp": 80,       # start renicing configured processes above this
    "throttle_recover_temp": 72,  # stop renicing once back below this
    "throttle_nice": 15,
    "throttle_processes": [],  # e.g. ["chromium", "firefox"]
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


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    cfg = load_config()
    MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(MODE_FILE.parent, 0o777)  # directory perms alone don't cover existing files
    if not MODE_FILE.exists():
        MODE_FILE.write_text("auto")
    if not LEVEL_FILE.exists():
        LEVEL_FILE.write_text("auto")
    os.chmod(MODE_FILE, 0o666)
    os.chmod(LEVEL_FILE, 0o666)
    if not LEVEL_FILE.exists():
        LEVEL_FILE.write_text("auto")

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

            if mode == "manual":
                lvl = read_manual_level()
                set_fan_level(lvl)
            else:
                current_level = level_for_temp(
                    cfg["curve"], temp, current_level, cfg["hysteresis"]
                )
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
