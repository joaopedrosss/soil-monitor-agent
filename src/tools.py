"""Ferramentas (somente leitura) que o agente pode chamar.

Cada ferramenta tem: uma função Python, que devolve um dict serializável em JSON, e um
schema JSON no formato de "function calling" da API OpenAI, que o LLM usa para decidir
quando e como chamá-la.

Nenhuma ferramenta aciona a irrigação: o agente responde perguntas, não controla
hardware. Atuação exigiria confirmação humana e está nos "próximos passos" do README.
"""
import json
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402

MAX_DAYS = 31
MAX_HOURLY_BUCKETS = 72
EPISODE_MERGE_GAP_S = 30  # pulsos de 10 s separados por pausas de 10 s formam um só evento


class ToolError(Exception):
    """Erro de uso da ferramenta: devolvido ao LLM para ele corrigir a chamada."""


def flask_live_reader(server):
    def read():
        with urllib.request.urlopen(server.rstrip("/") + "/api/dados", timeout=5) as r:
            return json.loads(r.read())
    return read


def db_live_reader(conn):
    """Modo demonstração (sem ESP32): usa a leitura mais recente do histórico como 'ao vivo'."""
    def read():
        r = conn.execute("SELECT umidade, temperatura, irrigando FROM leituras ORDER BY ts DESC LIMIT 1").fetchone()
        if r is None:
            return {"error": "Sem dados no histórico"}
        return {"umidade": r["umidade"], "temperatura": r["temperatura"], "irrigamento": bool(r["irrigando"])}
    return read


class SoilTools:
    def __init__(self, conn, live_reader=None, clock=None):
        self.conn = conn
        self.live_reader = live_reader
        self.clock = clock or (lambda: datetime.now(db.TZ))

    # ------------------------------------------------------------------ helpers
    def _window(self, inicio, fim):
        try:
            start, end = db.parse_local(inicio), db.parse_local(fim)
        except (ValueError, TypeError):
            raise ToolError("Datas inválidas. Use ISO 8601, ex.: '2026-11-14T00:00' (horário de Brasília).")
        if len(fim.strip()) == 10:  # só a data: inclui o dia inteiro
            end += timedelta(days=1)
        end = min(end, self.clock())
        if start >= end:
            raise ToolError("'inicio' deve ser anterior a 'fim' (e ao momento atual).")
        if end - start > timedelta(days=MAX_DAYS):
            raise ToolError(f"Período máximo: {MAX_DAYS} dias.")
        return int(start.timestamp()), int(end.timestamp())

    def _rows(self, t0, t1):
        return self.conn.execute(
            "SELECT ts, umidade, temperatura, irrigando FROM leituras WHERE ts >= ? AND ts < ? ORDER BY ts", (t0, t1)
        ).fetchall()

    def _last_row(self):
        return self.conn.execute("SELECT * FROM leituras ORDER BY ts DESC LIMIT 1").fetchone()

    # ------------------------------------------------------------------ tools
    def ler_sensores_agora(self):
        last = self._last_row()
        historico = None
        if last:
            historico = {"horario": db.fmt(last["ts"]), "umidade": last["umidade"], "temperatura": last["temperatura"],
                         "irrigando": bool(last["irrigando"])}
        if self.live_reader is None:
            return {"erro": "Leitura ao vivo não configurada.", "ultima_leitura_no_historico": historico}
        try:
            data = self.live_reader()
        except Exception as e:  # servidor fora do ar, rede, JSON inválido…
            return {"erro": f"Servidor do soil-monitor inacessível ({type(e).__name__}).",
                    "ultima_leitura_no_historico": historico}
        if "error" in data:
            return {"erro": f"O servidor respondeu sem dados: {data['error']}",
                    "ultima_leitura_no_historico": historico}
        return {
            "consultado_em": self.clock().strftime("%d/%m/%Y %H:%M"),
            "umidade": data["umidade"],
            "temperatura": data["temperatura"],
            "irrigando": bool(data["irrigamento"]),
            "limiar_irrigacao_umidade": db.UR_THRESHOLD,
            "ultima_leitura_no_historico": historico,
        }

    def resumo_periodo(self, inicio, fim, agrupar_por="dia"):
        if agrupar_por not in ("dia", "hora", "nenhum"):
            raise ToolError("agrupar_por deve ser 'dia', 'hora' ou 'nenhum'.")
        t0, t1 = self._window(inicio, fim)
        if agrupar_por == "hora" and (t1 - t0) / 3600 > MAX_HOURLY_BUCKETS:
            raise ToolError(f"Agrupamento por hora aceita no máximo {MAX_HOURLY_BUCKETS} h; use 'dia'.")
        rows = self._rows(t0, t1)
        if not rows:
            return {"periodo": f"{db.fmt(t0)} a {db.fmt(t1)}", "leituras": 0, "aviso": "Sem dados nesse período."}

        def key(ts):
            d = db.to_local(ts)
            return {"dia": d.strftime("%d/%m"), "hora": d.strftime("%d/%m %Hh"), "nenhum": "total"}[agrupar_por]

        buckets = {}
        for r in rows:
            buckets.setdefault(key(r["ts"]), []).append(r)

        def stats(rs):
            u = [r["umidade"] for r in rs]
            t = [r["temperatura"] for r in rs]
            umin = min(rs, key=lambda r: r["umidade"])
            tmax = max(rs, key=lambda r: r["temperatura"])
            return {
                "leituras": len(rs),
                "umidade_min": min(u), "umidade_media": round(sum(u) / len(u), 1), "umidade_max": max(u),
                "temperatura_min": min(t), "temperatura_media": round(sum(t) / len(t), 1), "temperatura_max": max(t),
                "quando_umidade_min": db.fmt(umin["ts"]), "quando_temperatura_max": db.fmt(tmax["ts"]),
                "pct_tempo_irrigando": round(100 * sum(r["irrigando"] for r in rs) / len(rs), 2),
            }

        out = {"periodo": f"{db.fmt(t0)} a {db.fmt(t1)}", "geral": stats(rows)}
        if agrupar_por != "nenhum":
            out["por_" + agrupar_por] = [{"periodo": k, **stats(v)} for k, v in buckets.items()]
        return out

    def _episodes(self, t0, t1):
        eps, cur = [], None
        for r in self._rows(t0, t1):
            if not r["irrigando"]:
                continue
            if cur and r["ts"] - cur["last"] <= EPISODE_MERGE_GAP_S:
                cur["last"] = r["ts"]
            else:
                cur = {"first": r["ts"], "last": r["ts"], "umidade_inicio": r["umidade"]}
                eps.append(cur)
        return eps

    def eventos_irrigacao(self, inicio, fim):
        t0, t1 = self._window(inicio, fim)
        eps = self._episodes(t0, t1)
        sample = self.conn.execute(
            "SELECT ts FROM leituras WHERE ts >= ? AND ts < ? ORDER BY ts LIMIT 2", (t0, t1)).fetchall()
        step = sample[1]["ts"] - sample[0]["ts"] if len(sample) == 2 else 5
        por_dia = {}
        for e in eps:
            d = db.to_local(e["first"]).strftime("%d/%m")
            por_dia[d] = por_dia.get(d, 0) + 1
        # dias com dados no período e sem nenhum evento
        # Brasil não tem horário de verão desde 2019: UTC-3 fixo
        dias_com_dados = [datetime.strptime(r[0], "%Y-%m-%d").strftime("%d/%m") for r in self.conn.execute(
            "SELECT DISTINCT date(ts, 'unixepoch', '-3 hours') d FROM leituras WHERE ts >= ? AND ts < ? ORDER BY d",
            (t0, t1))]
        total_s = sum(e["last"] - e["first"] + step for e in eps)
        lista = [{"inicio": db.fmt(e["first"]), "duracao_s": e["last"] - e["first"] + step,
                  "umidade_no_inicio": e["umidade_inicio"]} for e in eps]
        return {
            "periodo": f"{db.fmt(t0)} a {db.fmt(t1)}",
            "total_eventos": len(eps),
            "tempo_total_irrigando_min": round(total_s / 60, 1),
            "eventos_por_dia": por_dia,
            "dias_sem_irrigacao": [d for d in dias_com_dados if d not in por_dia],
            "eventos": lista[-30:],
            "observacao": ("Um evento = pulsos de irrigação seguidos (o firmware liga 10 s e desliga 10 s "
                           "enquanto a umidade está abaixo do limiar).") + (
                " Mostrando só os 30 últimos eventos." if len(lista) > 30 else ""),
        }

    def falhas_de_dados(self, inicio, fim, min_minutos=5):
        t0, t1 = self._window(inicio, fim)
        try:
            min_gap = max(1, float(min_minutos)) * 60
        except (TypeError, ValueError):
            raise ToolError("min_minutos deve ser um número.")
        ts = [r["ts"] for r in self._rows(t0, t1)]
        points = [t0] + ts + [t1]
        gaps = []
        for a, b in zip(points, points[1:]):
            if b - a >= min_gap:
                gaps.append({"de": db.fmt(a), "ate": db.fmt(b), "duracao_min": round((b - a) / 60)})
        return {"periodo": f"{db.fmt(t0)} a {db.fmt(t1)}", "falhas": gaps,
                "tempo_total_sem_dados_min": sum(g["duracao_min"] for g in gaps)}

    def info_sistema(self):
        first = self.conn.execute("SELECT MIN(ts) a, MAX(ts) b, COUNT(*) n FROM leituras").fetchone()
        fontes = [r[0] for r in self.conn.execute("SELECT DISTINCT fonte FROM leituras")]
        return {
            "hardware": "ESP32-WROVER com Zephyr RTOS; dois potenciômetros simulam os sensores de umidade e temperatura.",
            "envio": f"o ESP32 envia leituras ao servidor Flask a cada {db.ESP32_SEND_INTERVAL_S} s (HTTP POST).",
            "regra_de_irrigacao": (f"irriga quando a leitura bruta do sensor de umidade é < {db.IRRIGATION_ADC_THRESHOLD} "
                                   f"(≈ {db.UR_THRESHOLD}% de umidade): liga {db.IRRIGATION_ON_S} s, desliga "
                                   f"{db.IRRIGATION_OFF_S} s, e repete enquanto estiver abaixo do limiar."),
            "calibracao": "ADC 130 → 0% / 0 °C; ADC 2371 → 100% / 60 °C (interpolação linear).",
            "historico": {"desde": db.fmt(first["a"]) if first["a"] else None,
                          "ate": db.fmt(first["b"]) if first["b"] else None,
                          "leituras": first["n"], "fontes": fontes},
            "limitacoes": "não há previsão do tempo nem controle remoto da irrigação.",
        }

    # ------------------------------------------------------------------ dispatch
    def call(self, name, arguments):
        fn = getattr(self, name, None)
        if name not in TOOL_NAMES or fn is None:
            return {"erro": f"Ferramenta desconhecida: {name}"}
        try:
            args = json.loads(arguments) if isinstance(arguments, str) and arguments.strip() else (arguments or {})
            if not isinstance(args, dict):
                raise ToolError("Argumentos devem ser um objeto JSON.")
            return fn(**args)
        except ToolError as e:
            return {"erro": str(e)}
        except TypeError as e:
            return {"erro": f"Argumentos inválidos para {name}: {e}"}
        except json.JSONDecodeError:
            return {"erro": "Argumentos não são JSON válido."}


_PERIOD = {
    "inicio": {"type": "string", "description": "Início, ISO 8601 no horário de Brasília. Ex.: '2026-11-14T00:00' ou '2026-11-14'."},
    "fim": {"type": "string", "description": "Fim (exclusivo), ISO 8601. Uma data sem hora inclui o dia inteiro."},
}

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "ler_sensores_agora",
        "description": "Leitura atual (ao vivo) de umidade do solo, temperatura e se está irrigando agora.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "resumo_periodo",
        "description": ("Estatísticas do histórico num período: mínimo, média e máximo de umidade e temperatura, "
                        "quando ocorreram, e % do tempo irrigando. Pode agrupar por dia ou por hora."),
        "parameters": {"type": "object", "properties": {
            **_PERIOD,
            "agrupar_por": {"type": "string", "enum": ["dia", "hora", "nenhum"],
                            "description": "Granularidade. 'hora' aceita no máximo 72 h."}},
            "required": ["inicio", "fim"]}}},
    {"type": "function", "function": {
        "name": "eventos_irrigacao",
        "description": ("Eventos de irrigação num período: quantidade, duração total, contagem por dia, "
                        "dias sem irrigação e lista dos eventos."),
        "parameters": {"type": "object", "properties": _PERIOD, "required": ["inicio", "fim"]}}},
    {"type": "function", "function": {
        "name": "falhas_de_dados",
        "description": "Intervalos sem dados no histórico (ESP32 ou rede fora do ar) num período.",
        "parameters": {"type": "object", "properties": {
            **_PERIOD,
            "min_minutos": {"type": "number", "description": "Duração mínima de uma falha, em minutos (padrão 5)."}},
            "required": ["inicio", "fim"]}}},
    {"type": "function", "function": {
        "name": "info_sistema",
        "description": "Como o sistema funciona: hardware, regra e limiar de irrigação, calibração e período do histórico.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
]
TOOL_NAMES = {t["function"]["name"] for t in TOOL_SCHEMAS}
