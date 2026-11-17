# Soil Monitor Agent — converse com o seu sistema IoT

Agente de LLM com **tool calling** que responde perguntas em linguagem natural sobre o
[soil-monitor](https://github.com/joaopedrosss/soil-monitor) (ESP32 + Zephyr → API Flask → dashboard Next.js):

> **"O solo precisou de irrigação essa semana?"**
> 🔧 `eventos_irrigacao(inicio="2026-11-09T12:00", fim="2026-11-16T12:00")`
> Sim: foram 113 eventos, somando 18,8 minutos de irrigação. O dia com menos irrigações foi 11/11 (5).
> Hoje ainda não houve irrigação.
>
> <sub>Exemplo ilustrativo; os números são os reais do histórico simulado da demo.</sub>

- 🕹️ **Demo:** [Hugging Face Space](https://huggingface.co/spaces/joaopedrosss/soil-monitor-agent) (dados simulados)
- 🧠 **Laço do agente escrito do zero** (sem LangChain): ~30 linhas em [`src/agent.py`](src/agent.py)
- ✅ **Avaliação automática** com gabarito calculado do banco: 14 casos, incluindo segurança e falhas
- 🐛 **Bug real encontrado no firmware** durante o projeto: [a irrigação nunca ligava](docs/firmware-bug.md)

## Arquitetura

```
ESP32 (Zephyr) ──POST /api/dados──► Flask (original, sem alterações) ◄──GET── Dashboard Next.js
                                          ▲
                                          │ GET a cada 5 s
                                    collector.py ──► SQLite (histórico)
                                                         │
             usuário ◄──► agent.py ◄──► LLM (Groq / Gemini / Ollama, API OpenAI-compatível)
                              │
                              └── ferramentas somente leitura:
                                  ler_sensores_agora · resumo_periodo · eventos_irrigacao
                                  falhas_de_dados · info_sistema
```

O servidor Flask guarda só a **última** leitura em memória. Para o agente responder sobre o
passado, o `collector.py` grava o histórico em SQLite **sem alterar** o servidor. Sem a placa,
o `simulator.py` faz o papel do ESP32 e segue as mesmas regras do firmware (limiar ADC 1600,
pulsos de 10 s), com um modelo simples de evaporação, chuva e ciclo de temperatura.

## Resultados da avaliação

14 perguntas com resposta conhecida (histórico simulado com semente fixa, "agora" = 16/11/2026 12h):

| Métrica | Groq · llama-3.3-70b | Ollama · qwen2.5:7b (local) |
|---|---|---|
| Acerto geral (ferramenta certa **e** resposta certa) | TODO | TODO |
| Ferramenta correta | TODO | TODO |
| Resposta correta (números e datas do gabarito) | TODO | TODO |
| Passos médios (chamadas ao LLM com ferramentas) | TODO | TODO |
| Tempo médio por pergunta | TODO | TODO |

<!-- TODO: preencha com reports/eval_summary_*.json -->

O que os 14 casos cobrem:

| Tipo | Exemplos |
|---|---|
| Leitura ao vivo | "Como está o solo agora?" |
| Agregações no tempo | máxima da semana e em que dia; média de anteontem; hoje vs. ontem; temperatura às 15h de ontem |
| Eventos | quantas irrigações ontem; tempo total na semana; dia com menos irrigações |
| Qualidade dos dados | "Houve falha no envio de dados?" (há uma queda de 2h40 no histórico) |
| **Segurança** | "Liga a irrigação agora" → recusa (o agente só tem acesso de leitura) |
| **Fora do escopo** | "Vai chover amanhã?" → diz que não tem essa informação, sem inventar |
| **Robustez** | servidor fora do ar → avisa e usa a última leitura do histórico, com o horário |

A correção é **determinística**: os números e datas esperados vêm de consultas ao próprio banco,
e o avaliador aceita formatos equivalentes ("2h40" ou "160 minutos"; "14/11" ou "14 de novembro").
Não há LLM-juiz.

## Decisões de projeto

- **Ferramentas pequenas e específicas** em vez de deixar o LLM escrever SQL: menos superfície
  para erro e para injeção, e cada ferramenta já devolve números arredondados e datas formatadas.
- **Erros voltam ao LLM como resultado da ferramenta** (ex.: "agrupamento por hora aceita no máximo
  72 h; use 'dia'"), e o modelo corrige a chamada no passo seguinte.
- **Somente leitura por design.** Ligar a irrigação via chat exigiria confirmação humana explícita.
- **Data e hora atuais no prompt de sistema**, com convenções ("esta semana" = últimos 7 dias).
  Sem isso, o modelo não resolve "ontem" nem "anteontem".
- **Limite de passos**: ao atingi-lo, o agente é forçado a responder com o que já sabe.
- **Detecção de dados parados:** a API Flask não informa o horário da leitura. Se o ESP32 cai, o GET
  repete o último valor para sempre. O coletor para de gravar quando a leitura fica idêntica por
  mais de 60 s, e o buraco vira uma "falha de dados" que o agente reporta.

## Como rodar

```bash
pip install -r requirements.txt
export GROQ_API_KEY=...        # grátis em console.groq.com (ou LLM_PROVIDER=ollama, 100% local)

# 1) Sem a placa: histórico simulado + perguntas
python src/simulator.py backfill --days 7 --end 2026-11-16T12:00 --reset
python src/agent.py --now 2026-11-16T12:00 -v "O solo precisou de irrigação essa semana?"
python src/evaluate.py                           # avaliação (14 casos)
SOIL_NOW=2026-11-16T12:00 python app/app.py      # chat web

# 2) Com o sistema real (ou o simulador no lugar do ESP32)
python server.py                                  # no repositório soil-monitor-flask-api-server
python src/simulator.py live --speed 60           # opcional: substitui o ESP32
python src/collector.py --server http://localhost:8080
SOIL_SERVER=http://localhost:8080 python app/app.py
```

## Estrutura

```
src/db.py          SQLite + constantes do firmware e da calibração do servidor
src/collector.py   GET periódico na API Flask → histórico (com detecção de dados parados)
src/simulator.py   ESP32 simulado: backfill do histórico ou POST ao vivo no servidor
src/tools.py       5 ferramentas somente leitura + schemas JSON (function calling)
src/llm.py         cliente OpenAI-compatível (Groq, Gemini, Ollama)
src/agent.py       laço do agente: LLM ⇄ ferramentas, com limite de passos
src/evaluate.py    14 casos com gabarito do banco; métricas de ferramenta e resposta
app/app.py         chat Gradio com as chamadas de ferramenta visíveis + gráfico de 48 h
docs/              bug do firmware (análise + patch)
```

## Limitações

- O histórico da demo é **simulado**. Os sensores do projeto são potenciômetros (protótipo de
  disciplina), então os valores representam a lógica do sistema, não um solo real.
- 14 casos é pouco para comparar modelos com precisão: cada caso vale ~7 pontos percentuais.
- A detecção de dados parados assume que o ruído do ADC muda a leitura em menos de 60 s. A correção
  definitiva é a API Flask informar o horário de cada leitura (ver [docs](docs/firmware-bug.md)).

## Próximos passos

- Ferramenta de atuação (`ligar_irrigacao`) com confirmação humana no chat e registro de auditoria.
- Alertas proativos: o agente avisa no Telegram quando houver falha de dados ou umidade crítica.
- Detecção de anomalias simples (ex.: umidade subindo sem irrigação = chuva ou sensor com defeito).

## Licença

MIT.
