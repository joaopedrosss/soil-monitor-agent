---
title: Soil Monitor Agent
emoji: 🌱
colorFrom: green
colorTo: yellow
sdk: gradio
app_file: app.py
pinned: false
license: mit
short_description: Agente LLM com tool calling para um sistema IoT de irrigação
---

Demo do projeto https://github.com/joaopedrosss/soil-monitor-agent (dados simulados).

**Como publicar este Space:** suba para a raiz do Space `app.py`, este `README.md`, o
`requirements.txt` desta pasta e a pasta `src/` do repositório. No primeiro boot o app gera
7 dias de histórico simulado (~2 s).
Em *Settings → Variables and secrets*: secret `GROQ_API_KEY` e variável `SOIL_NOW=2026-11-16T12:00`
(fixa o "agora" no fim do histórico simulado).
