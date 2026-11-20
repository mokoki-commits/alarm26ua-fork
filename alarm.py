#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 okova
# Модернизация (исправление уровней/цветов тревоги, тесты): Cline (ИИ-ассистент), форк avk013/alarm26ua
"""Сервис тревоги: ukrainealarm API -> импульсы реле Tuya. Всё в одном файле.

Режимы:
    alarm.py          — сервис (опрос API, запуск паттернов)
    alarm.py on|off   — ручное включение/выключение реле (вместо on.py)
    alarm.py check    — диагностика: tinytuya, snapshot.json, выбор реле (реле не трогает)

Конфиги: config.txt (формат прежний) и snapshot.json (от мастера tinytuya).
Рассчитан на запуск из venv под systemd (Type=notify + WatchdogSec).

Защиты: таймауты на всё (HTTP, внешние команды), watchdog, контроль RSS,
лимиты на размер ответа и память дочерних процессов, гарантированное
выключение реле при любом выходе, сохранение состояния между рестартами.

Авторство:
    Идея и постановка задачи — okova.
    Аудит исходных скриптов, проектирование и реализация сервиса —
    Claude (ИИ-ассистент Anthropic) в совместной работе с okova.
Лицензия: GNU General Public License v2.0 (GPL-2.0-only), см. файл LICENSE.
Подробности — в README.md (паспорт проекта).
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import resource
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

__version__ = "1.0.2"

log = logging.getLogger("alarm")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("ALARM_CONFIG", BASE_DIR / "config.txt"))
SNAPSHOT_PATH = Path(os.environ.get("ALARM_SNAPSHOT", BASE_DIR / "snapshot.json"))
MAX_RESPONSE_BYTES = 1 << 20  # 1 МиБ — потолок ответа API
MAX_BACKOFF = 300             # потолок паузы при ошибках API, сек
TUYA_HARD_DEADLINE = 30       # жёсткий лимит жизни процесса `alarm.py on|off`
TUYA_ATTEMPTS = 3
EXIT_MEMORY = 70
EXIT_CONFIG = 78              # в unit: RestartPreventExitStatus=78

# От самого важного к наименее. AIR_* — воздушная тревога по уровням,
# AIR — воздушная без определённого уровня.
PRIORITY = [
    "URBAN_FIGHTS", "ARTILLERY", "NUCLEAR", "CHEMICAL",
    "AIR_RED", "AIR_ORANGE", "AIR_YELLOW", "AIR", "UNKNOWN",
]
AIR_LEVELS = ("RED", "ORANGE", "YELLOW")  # порядок = приоритет

Pattern = tuple[int, int, int, int]


class Stop(Exception):
    """Получен SIGTERM/SIGINT."""


class MemoryExceeded(Exception):
    """RSS процесса превысил лимит."""


class HardTimeout(Exception):
    """Сработал жёсткий дедлайн (SIGALRM)."""


_stop_requested = False


# --------------------------------------------------------------------------
# Конфиг (формат config.txt не изменён)
# --------------------------------------------------------------------------
def load_config(path: Path) -> dict[str, str]:
    cfg: dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            cfg[key.strip()] = value.strip()
    return cfg


def get_int(cfg: dict, key: str, default: int, lo: int, hi: int) -> int:
    value = int(cfg.get(key, default))
    if not lo <= value <= hi:
        raise ValueError(f"{key}={value} вне диапазона {lo}..{hi}")
    return value


def parse_pattern(value: str, max_on: int, max_count: int) -> Pattern:
    """Формат: первый_вкл,последующие_вкл,пауза,количество."""
    parts = [p.strip() for p in value.split(",")]
    if len(parts) != 4:
        raise ValueError(f"Неверный формат паттерна {value!r} (first_on,next_on,pause,count)")
    first_on, next_on, pause, count = (int(p) for p in parts)
    if first_on <= 0 or next_on <= 0 or pause < 0 or count <= 0:
        raise ValueError(f"Некорректные значения паттерна: {value!r}")
    # защита от опечаток, из-за которых реле останется включённым надолго
    if first_on > max_on or next_on > max_on:
        raise ValueError(f"Время ON в {value!r} больше MAX_ON_SECONDS={max_on}")
    if count > max_count:
        raise ValueError(f"Количество в {value!r} больше MAX_PULSE_COUNT={max_count}")
    return first_on, next_on, pause, count


@dataclass(frozen=True)
class Settings:
    api_key: str
    region_id: str
    poll_interval: int
    http_timeout: int
    http_deadline: int
    cmd_timeout: int
    cmd_mem_mb: int
    max_rss_mb: int
    state_max_age: int
    log_alert_data: bool
    log_data_max: int
    log_file: str
    log_max_mb: int
    log_backups: int
    cmd_on: str
    cmd_off: str
    pattern_clear: Pattern
    pattern_default: Pattern
    patterns: dict[str, Pattern]


def load_settings(cfg: dict[str, str]) -> Settings:
    for key in ("API_KEY", "REGION_ID", "CMD_ON", "CMD_OFF", "PATTERN_CLEAR"):
        if not cfg.get(key):
            raise ValueError(f"Не задан обязательный параметр {key}")

    max_on = get_int(cfg, "MAX_ON_SECONDS", 60, 1, 600)
    max_count = get_int(cfg, "MAX_PULSE_COUNT", 30, 1, 100)

    patterns: dict[str, Pattern] = {}
    for key, value in cfg.items():
        if key.startswith("PATTERN_") and key not in ("PATTERN_DEFAULT", "PATTERN_CLEAR"):
            try:
                patterns[key[len("PATTERN_"):]] = parse_pattern(value, max_on, max_count)
            except ValueError as e:
                log.warning("Пропускаю %s: %s", key, e)

    return Settings(
        api_key=cfg["API_KEY"],
        region_id=urllib.parse.quote(cfg["REGION_ID"], safe=""),
        poll_interval=get_int(cfg, "POLL_INTERVAL", 65, 30, 3600),
        http_timeout=get_int(cfg, "HTTP_TIMEOUT", 10, 1, 60),
        http_deadline=get_int(cfg, "HTTP_DEADLINE", 30, 5, 120),
        cmd_timeout=get_int(cfg, "CMD_TIMEOUT", 20, 1, 120),
        cmd_mem_mb=get_int(cfg, "CMD_MEM_MB", 512, 64, 4096),
        max_rss_mb=get_int(cfg, "MAX_RSS_MB", 80, 20, 2048),
        state_max_age=get_int(cfg, "STATE_MAX_AGE", 3600, 0, 86400),
        log_alert_data=cfg.get("LOG_ALERT_DATA", "0").strip().lower() in ("1", "true", "yes", "on"),
        log_data_max=get_int(cfg, "LOG_DATA_MAX", 4000, 200, 100000),
        log_file=cfg.get("LOG_FILE", "").strip(),
        log_max_mb=get_int(cfg, "LOG_MAX_MB", 5, 1, 1000),
        log_backups=get_int(cfg, "LOG_BACKUPS", 5, 0, 50),
        cmd_on=cfg["CMD_ON"],
        cmd_off=cfg["CMD_OFF"],
        pattern_clear=parse_pattern(cfg["PATTERN_CLEAR"], max_on, max_count),
        pattern_default=parse_pattern(cfg.get("PATTERN_DEFAULT", "5,3,5,2"), max_on, max_count),
        patterns=patterns,
    )


# --------------------------------------------------------------------------
# systemd watchdog / память / сон
# --------------------------------------------------------------------------
def sd_notify(message: str) -> None:
    """Минимальная реализация sd_notify без внешних зависимостей."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(message.encode())
    except OSError:
        pass


def current_rss_mb() -> float:
    try:
        with open("/proc/self/statm", "r") as f:
            pages = int(f.read().split()[1])
        return pages * resource.getpagesize() / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return 0.0


def checked_sleep(seconds: float, s: Settings) -> None:
    """Сон малыми шагами: реагирует на сигналы, шлёт watchdog, следит за памятью."""
    end = time.monotonic() + seconds
    while True:
        if _stop_requested:
            raise Stop
        rss = current_rss_mb()
        if rss > s.max_rss_mb:
            raise MemoryExceeded(f"RSS {rss:.0f} MiB > {s.max_rss_mb} MiB")
        sd_notify("WATCHDOG=1")
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def _on_term(signum, frame) -> None:
    global _stop_requested
    _stop_requested = True


def _on_alarm(signum, frame) -> None:
    raise HardTimeout("превышен жёсткий дедлайн операции")


# --------------------------------------------------------------------------
# Реле: запуск CMD_ON / CMD_OFF (как раньше, через shell) + встроенный Tuya
# --------------------------------------------------------------------------
def run_cmd(cmd: str, s: Settings) -> bool:
    """Команда из конфига (shell, как в оригинале) с таймаутом и лимитом памяти.
    При зависании убивается вся группа процессов."""
    limit = s.cmd_mem_mb * 1024 * 1024

    def preexec() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    sd_notify("WATCHDOG=1")
    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=preexec,
        )
    except OSError as e:
        log.error("Не удалось запустить %r: %s", cmd, e)
        return False

    try:
        out, _ = proc.communicate(timeout=s.cmd_timeout)
    except subprocess.TimeoutExpired:
        log.error("Команда зависла (> %ss), убиваю группу: %s", s.cmd_timeout, cmd)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        return False

    if proc.returncode != 0:
        tail = (out or b"")[-500:].decode("utf-8", "replace").strip()
        log.error("Команда %r завершилась с кодом %s: %s", cmd, proc.returncode, tail)
        return False
    return True


def ensure_off(s: Settings) -> bool:
    for attempt in range(1, 4):
        if run_cmd(s.cmd_off, s):
            return True
        log.warning("OFF не удался (попытка %d/3)", attempt)
        time.sleep(1)  # обычный sleep: вызывается и при остановке сервиса
    log.critical("Не удалось выключить реле!")
    return False


def load_tuya_device(cfg: dict[str, str]) -> tuple[dict, float]:
    """Читает snapshot.json и возвращает (устройство, версия протокола).
    Бросает OSError (нет файла / нет прав) или ValueError (битый JSON, нет устройства)."""
    data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("devices"), list):
        raise ValueError(f"{SNAPSHOT_PATH}: нет списка 'devices'")
    devices = [d for d in data["devices"] if isinstance(d, dict)]
    dev_id = cfg.get("TUYA_DEVICE_ID")
    if dev_id:
        dev = next((d for d in devices if d.get("id") == dev_id), None)
        if dev is None:
            raise ValueError(f"устройство {dev_id!r} не найдено в {SNAPSHOT_PATH}")
    elif len(devices) == 1:
        dev = devices[0]
    else:
        raise ValueError(
            "в snapshot.json несколько устройств (или ни одного): укажите TUYA_DEVICE_ID в config.txt. "
            "Доступные: " + ", ".join(f"{d.get('name')}={d.get('id')}" for d in devices))
    for field in ("id", "ip", "key"):
        if not dev.get(field):
            raise ValueError(f"у устройства в snapshot.json нет поля {field!r}")
    return dev, float(cfg.get("TUYA_VERSION") or dev.get("version") or 3.4)


def tuya_command(action: str) -> int:
    """`alarm.py on|off`: управление реле Tuya по данным из snapshot.json.

    Используйте в config.txt, например:
        CMD_ON=/opt/alarm/venv/bin/python /opt/alarm/alarm.py on
        CMD_OFF=/opt/alarm/venv/bin/python /opt/alarm/alarm.py off
    tinytuya импортируется только здесь — основной сервис остаётся лёгким.
    """

    def err(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    # жёсткий предохранитель от зависания (в т.ч. внутри сокетов tinytuya)
    signal.signal(signal.SIGALRM, lambda *_: os._exit(124))
    signal.alarm(TUYA_HARD_DEADLINE)

    try:
        import tinytuya
        cfg = load_config(CONFIG_PATH) if CONFIG_PATH.exists() else {}
        dev, version = load_tuya_device(cfg)
        device = tinytuya.OutletDevice(
            dev_id=dev["id"], address=dev["ip"], local_key=dev["key"], version=version
        )
    except ImportError as e:
        err(f"Не удалось импортировать tinytuya: {e}\n"
            f"  интерпретатор: {sys.executable}\n"
            f"  если пакета нет: {sys.executable} -m pip install -r /opt/alarm/requirements.txt")
        return EXIT_CONFIG
    except (OSError, ValueError) as e:
        err(f"Ошибка конфигурации: {type(e).__name__}: {e}")
        return EXIT_CONFIG

    if action == "check":
        print(f"OK: tinytuya {getattr(tinytuya, '__version__', '?')}, python {sys.executable}\n"
              f"    реле: {dev.get('name')} id={dev['id']} ip={dev['ip']} протокол={version}")
        return 0

    device.set_socketTimeout(5)
    device.set_socketRetryLimit(2)

    for attempt in range(1, TUYA_ATTEMPTS + 1):
        try:
            result = device.turn_on() if action == "on" else device.turn_off()
        except Exception as e:
            err(f"Попытка {attempt}/{TUYA_ATTEMPTS}: {type(e).__name__}: {e}")
        else:
            # tinytuya при сетевой ошибке не бросает исключение, а возвращает dict с 'Error'
            if isinstance(result, dict) and ("Error" in result or "Err" in result):
                err(f"Попытка {attempt}/{TUYA_ATTEMPTS}: {result}")
            else:
                return 0
        time.sleep(1)

    err(f"Команда '{action}' не выполнена")
    return 1


def pulse(s: Settings, pattern: Pattern) -> None:
    first_on, next_on, pause, count = pattern
    log.info("Импульс: %dx (первый %ds, далее %ds, пауза %ds)", count, first_on, next_on, pause)
    relay_on = False
    try:
        for i in range(count):
            on_s = first_on if i == 0 else next_on
            log.info("  цикл %d/%d: ON %ds", i + 1, count, on_s)
            relay_on = True  # считаем включённым даже если команда вернула ошибку
            run_cmd(s.cmd_on, s)
            checked_sleep(on_s, s)
            log.info("  цикл %d/%d: OFF", i + 1, count)
            relay_on = not ensure_off(s)
            if i < count - 1 and pause > 0:
                checked_sleep(pause, s)
    finally:
        # любой выход (SIGTERM, память, исключение) — реле не остаётся включённым
        if relay_on:
            ensure_off(s)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
def extract_types(data) -> list[str]:
    """Типы активных тревог. Воздушная раскладывается по уровню:
    AIR_RED / AIR_ORANGE / AIR_YELLOW, а без определённого уровня — AIR."""
    if not data or not isinstance(data, list):
        return []
    first = data[0]
    if not isinstance(first, dict):
        raise ValueError("неожиданная структура ответа")
    types: list[str] = []
    for alert in first.get("activeAlerts") or []:
        if not isinstance(alert, dict) or not alert.get("type"):
            continue
        t = str(alert["type"]).upper()
        if t == "AIR":
            levels = alert.get("activeAlertLevels") or []
            names: set[str] = set()
            for l in levels:
                if isinstance(l, dict):
                    # реальный ключ в API ukrainealarm — "alertLevel";
                    # оставляем fallback на "level" для старых/альтернативных ответов
                    val = l.get("alertLevel") or l.get("level") or ""
                    names.add(str(val).upper())
            # fallback: плоское поле на уровне alert
            if alert.get("level"):
                names.add(str(alert["level"]).upper())
            level = next((lv for lv in AIR_LEVELS if lv in names), None)
            t = f"AIR_{level}" if level else "AIR"
        types.append(t)
    return list(dict.fromkeys(types))  # уникальные, порядок сохранён


def choose_type(types: list[str]) -> str:
    return next((p for p in PRIORITY if p in types), types[0])


def fetch_alert_types(s: Settings) -> tuple[list[str], str] | None:
    """(типы, сырой JSON одной строкой). None — статус получить не удалось."""
    url = f"https://api.ukrainealarm.com/api/v3/alerts/{s.region_id}"
    req = urllib.request.Request(url, headers={
        "Authorization": s.api_key,
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    })
    # timeout у urlopen — на отдельную операцию сокета; SIGALRM ограничивает всё целиком
    signal.alarm(s.http_deadline)
    try:
        with urllib.request.urlopen(req, timeout=s.http_timeout) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            log.warning("Ответ API больше %d байт, игнорирую", MAX_RESPONSE_BYTES)
            return None
        data = json.loads(raw.decode("utf-8"))
        return extract_types(data), json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    except urllib.error.HTTPError as e:
        log.warning("HTTP %s: %s", e.code, e.reason)
    except (ValueError, UnicodeDecodeError) as e:
        log.warning("Некорректный ответ API: %s", e)
    except Exception as e:  # сеть, таймаут, HardTimeout
        log.warning("Ошибка запроса: %s: %s", type(e).__name__, e)
    finally:
        signal.alarm(0)
    return None


# --------------------------------------------------------------------------
# Состояние между рестартами
# --------------------------------------------------------------------------
def state_path() -> Path:
    base = os.environ.get("STATE_DIRECTORY", "").split(":")[0]
    return (Path(base) if base else BASE_DIR) / "state.json"


def load_state(s: Settings) -> tuple[bool, str | None] | None:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        if time.time() - float(data["ts"]) <= s.state_max_age:
            return bool(data["alert"]), data.get("type")
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def save_state(alert: bool, alert_type: str | None) -> None:
    path = state_path()
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(
            json.dumps({"alert": alert, "type": alert_type, "ts": time.time()}),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError as e:
        log.warning("Не удалось сохранить состояние: %s", e)


# --------------------------------------------------------------------------
# Главный цикл
# --------------------------------------------------------------------------
def setup_logging() -> None:
    fmt = "%(levelname)s %(message)s" if os.environ.get("JOURNAL_STREAM") \
        else "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, stream=sys.stdout)


def add_file_logging(s: Settings) -> None:
    """Дополнительно пишет лог в файл с ротацией (LOG_FILE). journald остаётся как есть."""
    if not s.log_file:
        return
    try:
        path = Path(s.log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=s.log_max_mb * 1024 * 1024, backupCount=s.log_backups,
            encoding="utf-8",
        )
    except OSError as e:
        log.warning("Лог в файл отключён: не удалось открыть %s: %s", s.log_file, e)
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)
    log.info("Лог дублируется в %s (ротация: %d МБ x %d)", s.log_file, s.log_max_mb, s.log_backups)


def run(s: Settings) -> None:
    log.info("Старт alarm v%s. Опрос каждые %ds, регион %s", __version__, s.poll_interval, s.region_id)
    log.info("Паттерны по типам: %s", s.patterns)
    log.info("По умолчанию: %s, отбой: %s", s.pattern_default, s.pattern_clear)

    was_alert: bool | None = None
    last_type: str | None = None
    restored = load_state(s)
    if restored is not None:
        was_alert, last_type = restored
        log.info("Восстановлено состояние: %s (%s)",
                 "тревога" if was_alert else "спокойно", last_type)

    fails = 0
    while True:
        result = fetch_alert_types(s)

        if result is None:
            fails += 1
            delay = min(s.poll_interval * 2 ** min(fails - 1, 3), MAX_BACKOFF)
            log.warning("Статус не получен (подряд ошибок: %d), следующая попытка через %ds",
                        fails, delay)
            checked_sleep(delay, s)
            continue
        fails = 0
        types, raw_json = result

        is_alert = bool(types)
        chosen = choose_type(types) if is_alert else None

        if s.log_alert_data and is_alert:
            shown = raw_json if len(raw_json) <= s.log_data_max \
                else raw_json[:s.log_data_max] + f"...[обрезано, всего {len(raw_json)} симв.]"
            log.info("[DATA] разобрано: %s -> выбран '%s'", types, chosen)
            log.info("[DATA] ответ API: %s", shown)

        if was_alert is None:
            log.info("Первый опрос: %s. Паттерны не запускаем.",
                     f"тревога ({chosen})" if is_alert else "спокойно")
            was_alert, last_type = is_alert, chosen
            save_state(is_alert, chosen)

        elif is_alert and not was_alert:
            pattern = s.patterns.get(chosen, s.pattern_default)
            log.warning("ТРЕВОГА (%s). Тип '%s' -> паттерн %s", ", ".join(types), chosen, pattern)
            pulse(s, pattern)
            was_alert, last_type = True, chosen
            save_state(True, chosen)

        elif not is_alert and was_alert:
            log.info("ОТБОЙ — выполняю паттерн отбоя")
            pulse(s, s.pattern_clear)
            was_alert, last_type = False, None
            save_state(False, None)

        elif is_alert and chosen != last_type:
            # смена типа внутри тревоги в любую сторону:
            # жёлтая <-> оранжевая <-> красная <-> неопределённая воздушная и т.д.
            pattern = s.patterns.get(chosen, s.pattern_default)
            log.warning("СМЕНА ТИПА: %s -> %s (%s). Паттерн %s",
                        last_type, chosen, ", ".join(types), pattern)
            pulse(s, pattern)
            last_type = chosen
            save_state(True, chosen)

        else:
            log.debug("Без изменений: %s", ", ".join(types) if is_alert else "спокойно")
            save_state(is_alert, last_type)  # обновляем метку времени: состояние всегда свежее

        checked_sleep(s.poll_interval, s)


def serve() -> int:
    setup_logging()
    try:
        cfg = load_config(CONFIG_PATH)
        settings = load_settings(cfg)
    except (OSError, ValueError) as e:
        log.critical("Ошибка конфигурации (%s): %s", CONFIG_PATH, e)
        return EXIT_CONFIG

    add_file_logging(settings)

    # если реле управляется встроенными `alarm.py on|off` — проверяем snapshot.json сразу,
    # а не в момент первой тревоги, когда уже поздно
    if "alarm.py" in settings.cmd_on or "alarm.py" in settings.cmd_off:
        import importlib.util
        if importlib.util.find_spec("tinytuya") is None:
            log.critical("tinytuya не найден для интерпретатора %s. Установите: "
                         "%s -m pip install -r /opt/alarm/requirements.txt",
                         sys.executable, sys.executable)
            return EXIT_CONFIG
        try:
            dev, version = load_tuya_device(cfg)
        except (OSError, ValueError) as e:
            log.critical("snapshot.json (%s) непригоден: %s", SNAPSHOT_PATH, e)
            return EXIT_CONFIG
        log.info("Реле: %s (id %s, ip %s, протокол %s)",
                 dev.get("name"), dev["id"], dev["ip"], version)

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    signal.signal(signal.SIGALRM, _on_alarm)

    sd_notify("READY=1")
    try:
        run(settings)
    except Stop:
        log.info("Получен сигнал остановки, выходим")
        return 0
    except MemoryExceeded as e:
        log.critical("Превышение памяти: %s. Выход для рестарта.", e)
        return EXIT_MEMORY
    except Exception:
        log.exception("Необработанная ошибка, выход для рестарта")
        return 1
    finally:
        sd_notify("STOPPING=1")


def main() -> int:
    if len(sys.argv) == 1:
        return serve()
    if len(sys.argv) == 2 and sys.argv[1] in ("on", "off", "check"):
        return tuya_command(sys.argv[1])
    print("Использование: alarm.py [on|off|check]", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main())
