"""Coletor: consulta o servidor Flask do soil-monitor e grava o histórico no SQLite.

O servidor original guarda só a última leitura em memória e não informa quando ela
foi recebida. Se o ESP32 parar de enviar, o GET continua devolvendo o último valor
para sempre. Por isso o coletor para de gravar quando a leitura fica idêntica por
mais de `--stale_after` segundos (com ruído real de ADC, isso só acontece se o ESP32
parou). Esses buracos no histórico viram "falhas de dados" que o agente sabe reportar.

Uso:
  python src/collector.py --server http://192.168.0.5:8080 --interval 5
"""
import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402


def fetch(server, timeout=5):
    with urllib.request.urlopen(server.rstrip("/") + "/api/dados", timeout=timeout) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://localhost:8080")
    ap.add_argument("--interval", type=float, default=5)
    ap.add_argument("--stale_after", type=float, default=60)
    ap.add_argument("--db", default=db.DEFAULT_DB)
    ap.add_argument("--max_samples", type=int, default=None, help="para testes")
    args = ap.parse_args()

    conn = db.connect(args.db)
    last_value, same_since, n = None, None, 0
    print(f"Coletando de {args.server} a cada {args.interval}s → {args.db}")
    while args.max_samples is None or n < args.max_samples:
        now = int(time.time())
        try:
            data = fetch(args.server)
            if "error" in data:
                print(f"[{db.fmt(now)}] servidor sem dados: {data['error']}")
            else:
                value = (float(data["umidade"]), float(data["temperatura"]), bool(data["irrigamento"]))
                if value != last_value:
                    last_value, same_since = value, now
                if now - same_since > args.stale_after:
                    print(f"[{db.fmt(now)}] leitura parada há {now - same_since}s — ESP32 provavelmente offline")
                else:
                    db.insert(conn, [(now, value[0], value[1], int(value[2]), "esp32")])
                    n += 1
        except (OSError, ValueError, KeyError) as e:
            print(f"[{db.fmt(now)}] falha ao consultar o servidor: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
