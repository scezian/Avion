#!/usr/bin/env bash
# setup.sh - installs the ThinkPad fan control daemon, CLI, and (optionally) the GUI.
# Run from the folder containing: fancontrold.py fanctl.py fancontrold.service config.toml fancontrol_gui.py

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

REQUIRED_FILES=(fancontrold.py fanctl.py fancontrold.service config.toml)
for f in "${REQUIRED_FILES[@]}"; do
    if [[ ! -f "$f" ]]; then
        echo "Missing $f in $SCRIPT_DIR - aborting." >&2
        exit 1
    fi
done

echo "==> Installing backend (daemon + CLI)"
sudo mkdir -p /etc/fancontrold /run/fancontrold
sudo cp config.toml /etc/fancontrold/config.toml
sudo cp fancontrold.py /usr/local/bin/fancontrold.py
sudo cp fanctl.py /usr/local/bin/fanctl
sudo chmod +x /usr/local/bin/fancontrold.py /usr/local/bin/fanctl
sudo cp fancontrold.service /etc/systemd/system/fancontrold.service

echo "==> Checking Python tomllib/tomli availability"
if ! python3 -c "import tomllib" 2>/dev/null; then
    if ! python3 -c "import tomli" 2>/dev/null; then
        echo "    Neither tomllib (py3.11+) nor tomli found - installing tomli"
        sudo pip install tomli --break-system-packages
    fi
fi

echo "==> Enabling and starting fancontrold service"
sudo systemctl daemon-reload
sudo systemctl enable --now fancontrold

echo "==> Fixing runtime directory/file permissions (belt-and-suspenders; the daemon"
echo "    now self-heals this on every start, but this covers the first run cleanly)"
sleep 1  # give the daemon a moment to create its runtime files
sudo chmod 777 /run/fancontrold || true
sudo touch /run/fancontrold/mode /run/fancontrold/level
sudo chmod 666 /run/fancontrold/mode /run/fancontrold/level

echo "==> Backend status:"
sudo fanctl status || true

echo
read -r -p "Install the GUI dashboard too? [Y/n] " install_gui
install_gui=${install_gui:-Y}

if [[ "$install_gui" =~ ^[Yy] ]]; then
    if [[ ! -f fancontrol_gui.py ]]; then
        echo "fancontrol_gui.py not found in $SCRIPT_DIR - skipping GUI install." >&2
    else
        echo "==> Installing PySide6 (this is a ~250MB download, may take a few minutes)"
        pip install PySide6 --break-system-packages

        echo "==> Installing psutil (powers the process picker in Throttled processes)"
        pip install psutil --break-system-packages

        echo "==> Setting up autostart entry"
        mkdir -p "$HOME/.config/autostart"
        GUI_PATH="$SCRIPT_DIR/fancontrol_gui.py"
        cat > "$HOME/.config/autostart/fancontrol-gui.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Fan Control
Exec=python3 $GUI_PATH
X-GNOME-Autostart-enabled=true
EOF
        echo "    Autostart entry written to ~/.config/autostart/fancontrol-gui.desktop"
        echo "    (edit or delete that file any time to change/disable autostart)"

        echo
        read -r -p "Launch the GUI now? [Y/n] " launch_now
        launch_now=${launch_now:-Y}
        if [[ "$launch_now" =~ ^[Yy] ]]; then
            nohup python3 "$GUI_PATH" >/tmp/fancontrol-gui.log 2>&1 &
            disown
            echo "    GUI launched (logs at /tmp/fancontrol-gui.log)"
        fi
    fi
else
    echo "Skipping GUI install. Run 'pip install PySide6 psutil --break-system-packages && python3 fancontrol_gui.py' later if you change your mind."
fi

echo
echo "==> Done."
echo "    Watch the daemon:   sudo journalctl -u fancontrold -f"
echo "    Manual override:    sudo fanctl set <0-7|full-speed|disengaged>"
echo "    Back to auto:       sudo fanctl auto"
echo "    Check status:       sudo fanctl status"
