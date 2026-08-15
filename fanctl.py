#!/usr/bin/env python3
"""
fanctl - manual override for fancontrold

Usage:
  fanctl auto              switch back to automatic temp-based control
  fanctl set <0-7|full-speed|disengaged>   force a fan level
  fanctl status            show current mode/level and live temp
"""

import sys
import re
from pathlib import Path

MODE_FILE = Path("/run/fancontrold/mode")
LEVEL_FILE = Path("/run/fancontrold/level")
FAN_PROC = Path("/proc/acpi/ibm/fan")
TEMP_PATH = Path("/sys/class/hwmon")


def find_package_temp_input():
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
    return None


def status():
    mode = MODE_FILE.read_text().strip() if MODE_FILE.exists() else "auto"
    level = LEVEL_FILE.read_text().strip() if LEVEL_FILE.exists() else "-"
    fan_status = FAN_PROC.read_text() if FAN_PROC.exists() else "unavailable"
    temp_input = find_package_temp_input()
    temp = f"{int(temp_input.read_text()) / 1000:.1f}C" if temp_input else "unknown"
    print(f"mode:  {mode}")
    print(f"level: {level if mode == 'manual' else '(auto-managed)'}")
    print(f"temp:  {temp}")
    print("--- /proc/acpi/ibm/fan ---")
    print(fan_status)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    cmd = sys.argv[1]

    if cmd == "auto":
        MODE_FILE.write_text("auto")
        print("Switched to automatic mode")
    elif cmd == "set":
        if len(sys.argv) < 3:
            print("Usage: fanctl set <0-7|full-speed|disengaged>")
            sys.exit(1)
        level = sys.argv[2]
        valid = level in ("full-speed", "disengaged") or (
            level.isdigit() and 0 <= int(level) <= 7
        )
        if not valid:
            print("Invalid level. Use 0-7, full-speed, or disengaged.")
            sys.exit(1)
        LEVEL_FILE.write_text(level)
        MODE_FILE.write_text("manual")
        print(f"Switched to manual mode, level={level}")
    elif cmd == "status":
        status()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
