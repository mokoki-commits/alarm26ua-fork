#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 okova
# alarm v1.0.1 — идея и постановка: okova; аудит и реализация: Claude (Anthropic)
# Модернизация (исправление уровней/цветов тревоги, тесты): Cline (ИИ-ассистент), форк avk013/alarm26ua
# Запуск: sudo bash install.sh   (из каталога с файлами сервиса)
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -euo pipefail

APP=/opt/alarm
SRC="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "Нужен root (sudo bash install.sh)"; exit 1; }

# группа и пользователь создаются явно (getent, без зависимости от настроек useradd)
if ! getent group alarm >/dev/null; then
    groupadd --system alarm
fi
if ! getent passwd alarm >/dev/null; then
    useradd --system --gid alarm --home-dir "$APP" --no-create-home --shell /usr/sbin/nologin alarm
fi

install -d -o root -g alarm -m 750 "$APP"
install -o root -g alarm -m 640 "$SRC/alarm.py" "$SRC/requirements.txt" "$APP/"

# venv: код принадлежит root, сервис его изменить не может
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --no-cache-dir --upgrade pip
"$APP/venv/bin/pip" install --no-cache-dir -r "$APP/requirements.txt"

if [ ! -f "$APP/config.txt" ]; then
    install -o root -g alarm -m 640 "$SRC/config.example.txt" "$APP/config.txt"
    echo ">>> Перенесите ваш config.txt в $APP/config.txt (формат прежний, CMD_ON/CMD_OFF см. config.example.txt)"
fi
if [ -f "$SRC/snapshot.json" ]; then
    install -o root -g alarm -m 640 "$SRC/snapshot.json" "$APP/snapshot.json"
else
    echo ">>> Скопируйте snapshot.json в $APP/ (chown root:alarm, chmod 640)"
fi

install -m 644 "$SRC/alarm.service" /etc/systemd/system/alarm.service
systemctl daemon-reload
systemctl enable alarm.service
echo "Готово. После настройки конфига: systemctl restart alarm && journalctl -fu alarm"
