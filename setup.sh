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

echo "==> Setting up the 'fancontrol' group"
echo "    (lets the GUI change modes/read logs without sudo)"
NEEDS_RELOGIN=0
if ! getent group fancontrol > /dev/null; then
    sudo groupadd fancontrol
    echo "    Created 'fancontrol' group"
fi
if ! id -nG "$USER" | grep -qw fancontrol; then
    sudo usermod -aG fancontrol "$USER"
    NEEDS_RELOGIN=1
    echo "    Added $USER to 'fancontrol' group"
else
    echo "    $USER is already in the 'fancontrol' group"
fi

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

        echo "==> Installing systemd user service for the GUI"
        mkdir -p "$HOME/.config/systemd/user"
        GUI_PATH="$SCRIPT_DIR/fancontrol_gui.py"
        PYTHON_BIN="$(command -v python3)"
        cat > "$HOME/.config/systemd/user/fancontrol-gui.service" <<EOF
[Unit]
Description=Fan Control GUI dashboard
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=$PYTHON_BIN $GUI_PATH
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
EOF
        systemctl --user daemon-reload
        systemctl --user enable fancontrol-gui.service
        echo "    Installed ~/.config/systemd/user/fancontrol-gui.service (enabled)"

        # A systemd --user service only works if the graphical session's
        # environment (WAYLAND_DISPLAY, etc.) has been imported into the
        # systemd user manager. Hyprland doesn't do this by default, so we
        # add one line to hyprland.conf if it's missing.
        HYPR_CONF="$HOME/.config/hypr/hyprland.conf"
        IMPORT_LINE='exec-once = systemctl --user import-environment WAYLAND_DISPLAY XDG_CURRENT_DESKTOP DISPLAY && systemctl --user start fancontrol-gui.service'
        if [[ -f "$HYPR_CONF" ]]; then
            if ! grep -qF "fancontrol-gui.service" "$HYPR_CONF"; then
                echo "" >> "$HYPR_CONF"
                echo "# Added by Avion fan control setup - imports session env into" >> "$HYPR_CONF"
                echo "# systemd --user so the fancontrol-gui service can actually show a window" >> "$HYPR_CONF"
                echo "$IMPORT_LINE" >> "$HYPR_CONF"
                echo "    Added an exec-once line to $HYPR_CONF"
            else
                echo "    hyprland.conf already references fancontrol-gui.service - left untouched"
            fi
        else
            echo "    Could not find $HYPR_CONF automatically."
            echo "    Add this line to your Hyprland config yourself so the service can start:"
            echo "      $IMPORT_LINE"
        fi

        echo "==> Adding 'avion' alias"
        SHELL_RC=""
        case "$SHELL" in
            */zsh) SHELL_RC="$HOME/.zshrc" ;;
            */bash) SHELL_RC="$HOME/.bashrc" ;;
        esac
        if [[ -n "$SHELL_RC" ]]; then
            if ! grep -qF "alias avion=" "$SHELL_RC" 2>/dev/null; then
                echo "" >> "$SHELL_RC"
                echo "# Avion fan control dashboard" >> "$SHELL_RC"
                echo "alias avion='systemctl --user restart fancontrol-gui.service'" >> "$SHELL_RC"
                echo "    Added 'avion' alias to $SHELL_RC (restart your shell, or run: source $SHELL_RC)"
            else
                echo "    'avion' alias already present in $SHELL_RC - left untouched"
            fi
        else
            echo "    Couldn't detect zsh/bash from \$SHELL ($SHELL) - add this alias yourself:"
            echo "      alias avion='systemctl --user restart fancontrol-gui.service'"
        fi

        echo
        read -r -p "Start the GUI service now? [Y/n] " launch_now
        launch_now=${launch_now:-Y}
        if [[ "$launch_now" =~ ^[Yy] ]]; then
            systemctl --user start fancontrol-gui.service
            sleep 1
            systemctl --user --no-pager status fancontrol-gui.service || true
        fi
    fi
else
    echo "Skipping GUI install. Run 'pip install PySide6 psutil --break-system-packages && python3 fancontrol_gui.py' later if you change your mind."
fi

echo
echo "==> Done."
if [[ "$NEEDS_RELOGIN" == "1" ]]; then
    echo "    ⚠ You were just added to the 'fancontrol' group - this only takes"
    echo "      effect after you LOG OUT AND BACK IN (a new terminal isn't enough)."
    echo "      Until then the GUI will need sudo to change Auto/Manual mode."
fi
echo "    Watch the daemon:      sudo journalctl -u fancontrold -f"
echo "    Manual override:       sudo fanctl set <0-7|full-speed|disengaged>"
echo "    Back to auto:          sudo fanctl auto"
echo "    Check status:          sudo fanctl status"
echo "    GUI service status:    systemctl --user status fancontrol-gui.service"
echo "    Relaunch/show the GUI: avion   (after restarting your shell)"
echo
echo "    IMPORTANT: reboot once, then confirm the GUI actually autostarted with:"
echo "      systemctl --user status fancontrol-gui.service"
echo "    It should show 'active (running)'. If it shows 'inactive' or 'failed',"
echo "    the Hyprland env-import line likely isn't running yet - check"
echo "    $HOME/.config/hypr/hyprland.conf and see the README's autostart section."
