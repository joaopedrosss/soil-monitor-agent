"""Chat com o agente do soil-monitor (Gradio).

Variáveis de ambiente:
  SOIL_DB      caminho do SQLite (padrão data/soil.db)
  SOIL_SERVER  URL do servidor Flask (ex.: http://192.168.0.5:8080). Vazio = modo demonstração,
               em que a "leitura ao vivo" é a última linha do histórico.
  SOIL_NOW     fixa o "agora" (ex.: 2026-11-16T12:00), para conversar com dados simulados
  + as variáveis do LLM (LLM_PROVIDER, GROQ_API_KEY etc.; ver src/llm.py)

Local:  python app/app.py
"""
import inspect
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if not (ROOT / "src").exists():  # no Space, src/ e data/ ficam ao lado de app.py
    ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)

import gradio as gr  # noqa: E402
import pandas as pd  # noqa: E402

import db  # noqa: E402
from agent import SoilAgent  # noqa: E402
from llm import LLM  # noqa: E402
from tools import SoilTools, db_live_reader, flask_live_reader  # noqa: E402

CONN = db.connect(os.environ.get("SOIL_DB", db.DEFAULT_DB))
SERVER = os.environ.get("SOIL_SERVER", "").strip()
NOW = os.environ.get("SOIL_NOW", "").strip()
CLOCK = (lambda: db.parse_local(NOW)) if NOW else None
if not SERVER and not CONN.execute("SELECT 1 FROM leituras LIMIT 1").fetchone():
    # Modo demonstração com banco vazio (ex.: primeiro boot no Hugging Face Spaces): gera 7 dias simulados.
    from simulator import seed_history
    seed_history(CONN, db.parse_local(NOW) if NOW else datetime.now(db.TZ).replace(microsecond=0))
TOOLS = SoilTools(CONN, live_reader=flask_live_reader(SERVER) if SERVER else db_live_reader(CONN), clock=CLOCK)
AGENT = SoilAgent(LLM(), TOOLS)
MODE = f"servidor {SERVER}" if SERVER else "demonstração (dados simulados)"


def chart_data(hours=48):
    end = TOOLS.clock()
    t0 = int((end - timedelta(hours=hours)).timestamp())
    df = pd.read_sql_query(
        "SELECT ts, umidade, temperatura FROM leituras WHERE ts >= ? AND ts <= ? AND ts % 300 < 5 ORDER BY ts",
        CONN, params=(t0, int(end.timestamp())))
    df["horario"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert(db.TZ).dt.tz_localize(None)
    long = df.melt(id_vars="horario", value_vars=["umidade", "temperatura"], var_name="série", value_name="valor")
    limiar = pd.DataFrame({"horario": df["horario"], "série": f"limiar de irrigação ({db.UR_THRESHOLD}%)",
                           "valor": db.UR_THRESHOLD})
    return pd.concat([long, limiar], ignore_index=True)


def respond(message, chat):
    history = [{"role": m["role"], "content": m["content"]} for m in chat
               if isinstance(m.get("content"), str) and not (m.get("metadata") or {}).get("title")]
    chat = chat + [{"role": "user", "content": message}]
    yield chat, ""
    try:
        out = AGENT.run(message, history=history)
    except Exception as e:
        chat.append({"role": "assistant", "content": f"Erro ao consultar o LLM: {type(e).__name__}: {e}"})
        yield chat, ""
        return
    for t in out["trace"]:
        result = json.dumps(t["result"], ensure_ascii=False)
        chat.append({"role": "assistant", "metadata": {"title": f"🔧 {t['tool']}"},
                     "content": f"argumentos: `{t['arguments'] or '{}'}`\n\nresultado: `{result[:600]}`"})
    chat.append({"role": "assistant", "content": out["answer"]})
    yield chat, ""


chat_kwargs = {"type": "messages"} if "type" in inspect.signature(gr.Chatbot.__init__).parameters else {}

with gr.Blocks(title="Soil Monitor Agent") as demo:
    gr.Markdown(f"# 🌱 Soil Monitor — agente\nPergunte sobre o solo em linguagem natural. Modo: **{MODE}**. "
                "O agente só tem acesso de leitura: consulta o sensor e o histórico, mas não aciona a irrigação.")
    with gr.Row():
        with gr.Column(scale=3):
            chatbot = gr.Chatbot(height=480, label="Conversa", **chat_kwargs)
            box = gr.Textbox(placeholder="Ex.: O solo precisou de irrigação essa semana?", show_label=False)
            gr.Examples(["Como está o solo agora?", "Quantas vezes o sistema irrigou ontem?",
                         "Houve alguma falha no envio de dados nos últimos 3 dias?",
                         "Compare a umidade média de hoje com a de ontem.", "Liga a irrigação agora."], inputs=box)
        with gr.Column(scale=2):
            gr.LinePlot(chart_data(), x="horario", y="valor", color="série", title="Últimas 48 h", height=420)
    box.submit(respond, [box, chatbot], [chatbot, box])

if __name__ == "__main__":
    demo.launch()
