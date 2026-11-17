"""Agente de tool calling para o soil-monitor (laço implementado à mão, sem framework).

Laço:
  1. Envia ao LLM o histórico da conversa + os schemas das ferramentas.
  2. Se o LLM pedir ferramentas, executa cada uma, devolve o resultado como mensagem
     `role="tool"` e volta ao passo 1.
  3. Se o LLM responder em texto, essa é a resposta final.
  Limite de passos para evitar laços infinitos; ao atingir o limite, força uma resposta.

Uso:
  python src/agent.py "O solo precisou de irrigação essa semana?"
  python src/agent.py --server http://192.168.0.5:8080 "Como está o solo agora?"
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402
from tools import TOOL_SCHEMAS, SoilTools, flask_live_reader  # noqa: E402

WEEKDAYS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]


def system_prompt(now: datetime) -> str:
    return f"""Você é o assistente do soil-monitor, um sistema IoT de monitoramento e irrigação de solo
(ESP32 + servidor Flask). Responde perguntas do usuário usando as ferramentas disponíveis.

Agora: {WEEKDAYS[now.weekday()]}, {now:%d/%m/%Y %H:%M} (horário de Brasília).
- "hoje" = {now:%Y-%m-%d}; "esta semana" / "últimos dias" sem número = últimos 7 dias até agora.
- Nas ferramentas, use datas ISO 8601 no horário de Brasília (ex.: '{now:%Y-%m-%d}T00:00').

Regras:
1. Todo número na resposta deve vir de uma ferramenta chamada nesta conversa. Nunca estime ou invente.
2. Responda em português do Brasil, de forma direta (2 a 5 frases). Datas como dd/mm, horas como 14h30,
   umidade em % com uma casa decimal e temperatura em °C.
3. Se a ferramenta indicar falhas de dados ou "Sem dados", diga isso em vez de tirar conclusões.
4. Se a leitura ao vivo falhar, informe e ofereça a última leitura do histórico com o horário dela.
5. Você só tem acesso de LEITURA. Se pedirem para ligar/desligar a irrigação ou mudar configurações,
   explique que não pode fazer isso.
6. Se a pergunta não puder ser respondida com as ferramentas (ex.: previsão do tempo), diga que não tem
   essa informação, sem chamar ferramentas desnecessárias."""


class SoilAgent:
    def __init__(self, llm, tools: SoilTools, max_steps=6):
        self.llm, self.tools, self.max_steps = llm, tools, max_steps

    def run(self, question: str, history=None) -> dict:
        """history: lista de mensagens {'role': 'user'|'assistant', 'content': str} de turnos anteriores."""
        t_start = time.time()
        messages = [{"role": "system", "content": system_prompt(self.tools.clock())}]
        messages += list(history or [])
        messages.append({"role": "user", "content": question})
        trace = []

        for step in range(self.max_steps + 1):
            force_answer = step == self.max_steps
            msg = self.llm.complete(messages, tools=TOOL_SCHEMAS, tool_choice="none" if force_answer else "auto")
            calls = msg.tool_calls or []
            if not calls:
                return {"answer": (msg.content or "").strip(), "trace": trace, "steps": step,
                        "seconds": round(time.time() - t_start, 1)}

            messages.append({
                "role": "assistant",
                "content": msg.content or None,
                "tool_calls": [{"id": c.id, "type": "function",
                                "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
                               for c in calls],
            })
            for c in calls:
                result = self.tools.call(c.function.name, c.function.arguments)
                trace.append({"tool": c.function.name, "arguments": c.function.arguments, "result": result})
                messages.append({"role": "tool", "tool_call_id": c.id,
                                 "content": json.dumps(result, ensure_ascii=False)})
        raise RuntimeError("inalcançável")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--db", default=db.DEFAULT_DB)
    ap.add_argument("--server", default="http://localhost:8080", help="servidor Flask do soil-monitor")
    ap.add_argument("--now", default=None, help="fixa o 'agora' (ex.: 2026-11-16T12:00), útil com dados simulados")
    ap.add_argument("-v", "--verbose", action="store_true", help="mostra as chamadas de ferramenta")
    args = ap.parse_args()

    from llm import LLM

    clock = (lambda: db.parse_local(args.now)) if args.now else None
    tools = SoilTools(db.connect(args.db), live_reader=flask_live_reader(args.server), clock=clock)
    out = SoilAgent(LLM(), tools).run(args.question)
    if args.verbose:
        for t in out["trace"]:
            print(f"🔧 {t['tool']}({t['arguments']})")
            print("   →", json.dumps(t["result"], ensure_ascii=False)[:300])
    print("\n" + out["answer"])


if __name__ == "__main__":
    main()
