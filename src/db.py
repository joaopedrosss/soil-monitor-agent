"""Armazenamento do histórico de leituras em SQLite e constantes do sistema físico.

As conversões ADC → unidade espelham o conversor.py do servidor Flask, e o limiar de
irrigação espelha o firmware (led.c: `ultima_umidade < 1600`).
"""
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")
DEFAULT_DB = "data/soil.db"

# --- Calibração (soil-monitor-flask-api-server/conversor.py) ---
ADC_MIN, ADC_MAX = 130, 2371
UR_RANGE = (0.0, 100.0)
TEMP_RANGE = (0.0, 60.0)

# --- Firmware (soil-monitor-esp32-firmware/src/led.c e main.c) ---
IRRIGATION_ADC_THRESHOLD = 1600  # irriga quando a leitura do Pot1 < 1600
IRRIGATION_ON_S = 10  # SLEEP_TIME_ON_MS
IRRIGATION_OFF_S = 10  # SLEEP_TIME_OFF_MS
ESP32_SEND_INTERVAL_S = 3.5  # k_msleep(3500) no laço principal


def _interp(adc, lo, hi):
    if adc is None:
        return 0.0
    v = lo + (float(adc) - ADC_MIN) * (hi - lo) / (ADC_MAX - ADC_MIN)
    return round(max(lo, min(hi, v)), 1)


def to_ur(adc):
    return _interp(adc, *UR_RANGE)


def to_celsius(adc):
    return _interp(adc, *TEMP_RANGE)


def adc_from(value, lo, hi):
    return int(round(ADC_MIN + (value - lo) * (ADC_MAX - ADC_MIN) / (hi - lo)))


UR_THRESHOLD = to_ur(IRRIGATION_ADC_THRESHOLD)  # ≈ 65.6 %

SCHEMA = """
CREATE TABLE IF NOT EXISTS leituras (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,      -- epoch (s, UTC)
    umidade     REAL    NOT NULL,      -- % UR
    temperatura REAL    NOT NULL,      -- °C
    irrigando   INTEGER NOT NULL,      -- 0/1
    fonte       TEXT    NOT NULL       -- 'esp32' (coletado da API) ou 'simulado'
);
CREATE INDEX IF NOT EXISTS idx_leituras_ts ON leituras(ts);
"""


def connect(path=DEFAULT_DB):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def insert(conn, rows):
    """rows: iterável de (ts, umidade, temperatura, irrigando, fonte)."""
    conn.executemany(
        "INSERT INTO leituras (ts, umidade, temperatura, irrigando, fonte) VALUES (?, ?, ?, ?, ?)", rows
    )
    conn.commit()


def to_local(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, TZ)


def parse_local(text: str) -> datetime:
    """Aceita '2026-11-14', '2026-11-14T15:00' ou ISO com fuso; sem fuso = horário de Brasília."""
    dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def fmt(ts: int) -> str:
    return to_local(ts).strftime("%d/%m/%Y %H:%M")
