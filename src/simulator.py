"""Simulador do ESP32 do soil-monitor, para demonstrar e avaliar o agente sem a placa.

Reproduz as regras do firmware (com o bug das variáveis `static` corrigido, ver docs/):
  - lê os "sensores" a cada 3,5 s e envia {potenciometro1, potenciometro2, irrigando};
  - a thread de irrigação liga por 10 s e desliga por 10 s enquanto Pot1 < 1600.

Sobre isso há um modelo físico simples do solo, para o histórico ter algo a contar:
temperatura com ciclo diário (Maceió, ~22–34 °C), umidade que cai com a evaporação
(mais rápido no calor), sobe durante a irrigação e com chuvas ocasionais.

Modos:
  python src/simulator.py backfill --days 7          # grava 7 dias direto no SQLite (fonte='simulado')
  python src/simulator.py live --server http://localhost:8080 --speed 60
      # envia POST /api/dados ao servidor Flask original, como o ESP32 faria;
      # --speed 60 = cada segundo real avança 1 minuto simulado
"""
import argparse
import json
import math
import random
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402


class SoilModel:
    def __init__(self, seed=7, start_moisture=78.0):
        self.rng = random.Random(seed)
        self.moisture = start_moisture
        self.day_offset = {}
        self.rain = {}  # data -> (início_h, duração_h) ou None

    def _day(self, local_dt):
        d = local_dt.date()
        if d not in self.day_offset:
            self.day_offset[d] = self.rng.gauss(0, 1.3)
            self.rain[d] = (self.rng.uniform(1, 20), self.rng.uniform(0.7, 2.5)) if self.rng.random() < 0.3 else None
        return d

    def temperature(self, local_dt):
        d = self._day(local_dt)
        h = local_dt.hour + local_dt.minute / 60
        return 28 + 5 * math.sin(2 * math.pi * (h - 9) / 24) + self.day_offset[d]

    def raining(self, local_dt):
        r = self.rain[self._day(local_dt)]
        h = local_dt.hour + local_dt.minute / 60
        return r is not None and r[0] <= h < r[0] + r[1]

    def step(self, local_dt, dt_s, irrigating):
        temp = self.temperature(local_dt)
        evap_per_h = max(0.2, 1.1 * (1 + (temp - 28) / 12))
        self.moisture -= evap_per_h * dt_s / 3600
        if irrigating:
            self.moisture += 0.12 * dt_s
        if self.raining(local_dt):
            self.moisture += 0.02 * dt_s
        self.moisture = max(5.0, min(96.0, self.moisture))
        return temp

    def read_adc(self, local_dt):
        temp = self.temperature(local_dt) + self.rng.gauss(0, 0.25)
        ur = self.moisture + self.rng.gauss(0, 0.35)
        return db.adc_from(ur, *db.UR_RANGE), db.adc_from(temp, *db.TEMP_RANGE)


class Firmware:
    """Máquina de estados equivalente à led_thread_function() + laço do main()."""

    def __init__(self):
        self.last_pot1 = 2000  # valor inicial seguro, como no firmware
        self.led_on = False
        self.phase_left = 0.0
        self.phase = "check"

    def tick(self, dt_s):
        self.phase_left -= dt_s
        if self.phase_left > 0:
            return
        if self.phase == "on":
            self.led_on, self.phase, self.phase_left = False, "off", db.IRRIGATION_OFF_S
        elif self.last_pot1 < db.IRRIGATION_ADC_THRESHOLD:
            self.led_on, self.phase, self.phase_left = True, "on", db.IRRIGATION_ON_S
        else:
            self.led_on, self.phase, self.phase_left = False, "off", db.IRRIGATION_OFF_S


def simulate(start, end, step_s=1, sample_every_s=5, seed=7, outages=()):
    """Gera amostras (ts, umidade, temperatura, irrigando) entre start e end (datetimes locais)."""
    model, fw = SoilModel(seed), Firmware()
    t, next_read, next_sample = start, start, start
    while t < end:
        if t >= next_read:  # laço principal do ESP32: lê ADC a cada 3,5 s
            pot1, pot2 = model.read_adc(t)
            fw.last_pot1 = pot1
            next_read = t + timedelta(seconds=db.ESP32_SEND_INTERVAL_S)
        fw.tick(step_s)
        model.step(t, step_s, fw.led_on)
        if t >= next_sample:
            if not any(a <= t < b for a, b in outages):
                yield int(t.timestamp()), db.to_ur(pot1), db.to_celsius(pot2), int(fw.led_on), pot1, pot2
            next_sample = t + timedelta(seconds=sample_every_s)
        t += timedelta(seconds=step_s)


def seed_history(conn, end, days=7, sample_every=5, seed=7, reset=False):
    """Gera `days` dias de histórico simulado terminando em `end` (datetime local)."""
    start = (end - timedelta(days=days)).replace(hour=0, minute=0, second=0)
    # Uma queda de conexão de ~2h40 no penúltimo dia, para o agente ter falhas a reportar.
    gap_day = (end - timedelta(days=2)).replace(hour=9, minute=10, second=0)
    outages = [(gap_day, gap_day + timedelta(hours=2, minutes=40))]
    if reset:
        conn.execute("DELETE FROM leituras")
    rows = [(ts, u, t, irr, "simulado") for ts, u, t, irr, _, _ in
            simulate(start, end, sample_every_s=sample_every, seed=seed, outages=outages)]
    db.insert(conn, rows)
    return start, len(rows)


def backfill(args):
    end = db.parse_local(args.end) if args.end else datetime.now(db.TZ).replace(microsecond=0)
    start, n = seed_history(db.connect(args.db), end, args.days, args.sample_every, args.seed, args.reset)
    print(f"{n} leituras simuladas de {start:%d/%m %H:%M} a {end:%d/%m %H:%M} → {args.db}")


def live(args):
    url = args.server.rstrip("/") + "/api/dados"
    start = datetime.now(db.TZ)
    gen = simulate(start, start + timedelta(days=365), step_s=1, sample_every_s=db.ESP32_SEND_INTERVAL_S,
                   seed=args.seed)
    print(f"Enviando para {url} a cada {db.ESP32_SEND_INTERVAL_S}s (velocidade {args.speed}x). Ctrl+C para parar.")
    per_tick = max(1, round(args.speed))
    while True:
        for _ in range(per_tick):
            _, ur, temp, irr, pot1, pot2 = next(gen)
        body = json.dumps({"potenciometro1": pot1, "potenciometro2": pot2, "irrigando": bool(irr)}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5).read()
            print(f"[SIM] Pot1={pot1} ({ur}%) Pot2={pot2} ({temp}°C) irrigando={bool(irr)}")
        except OSError as e:
            print(f"[SIM] Falha ao enviar: {e}")
        time.sleep(db.ESP32_SEND_INTERVAL_S)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    b = sub.add_parser("backfill")
    b.add_argument("--days", type=int, default=7)
    b.add_argument("--end", default=None, help="fim do histórico (ex.: 2026-11-16T12:00); padrão: agora")
    b.add_argument("--sample_every", type=int, default=5, help="intervalo de gravação (s), como o coletor")
    b.add_argument("--seed", type=int, default=7)
    b.add_argument("--db", default=db.DEFAULT_DB)
    b.add_argument("--reset", action="store_true", help="apaga o histórico antes")
    lv = sub.add_parser("live")
    lv.add_argument("--server", default="http://localhost:8080")
    lv.add_argument("--speed", type=float, default=1)
    lv.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    backfill(args) if args.mode == "backfill" else live(args)


if __name__ == "__main__":
    main()
