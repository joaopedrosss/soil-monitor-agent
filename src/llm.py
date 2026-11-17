"""Cliente para LLMs com API compatível com a da OpenAI (incluindo function calling).

  LLM_PROVIDER = groq | gemini | ollama | custom   (padrão: groq)
  LLM_MODEL    = sobrescreve o modelo padrão do provedor
  LLM_API_KEY  = chave (ou GROQ_API_KEY / GEMINI_API_KEY)
  LLM_BASE_URL = obrigatório só para "custom"

O modelo precisa suportar tool calling. Os nomes de modelos mudam com frequência:
se um padrão deixar de existir, defina LLM_MODEL com um listado no painel do provedor.
"""
import os
import time

import openai
from openai import OpenAI

PROVIDERS = {
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY",
             "model": "llama-3.3-70b-versatile"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "key_env": "GEMINI_API_KEY",
               "model": "gemini-2.5-flash"},
    "ollama": {"base_url": "http://localhost:11434/v1", "key_env": None, "model": "qwen2.5:7b"},
    "custom": {"base_url": None, "key_env": None, "model": None},
}

RETRYABLE = (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError)


class LLM:
    def __init__(self, provider=None, model=None, temperature=0.0):
        self.provider = provider or os.environ.get("LLM_PROVIDER", "groq")
        cfg = PROVIDERS[self.provider]
        base_url = os.environ.get("LLM_BASE_URL") or cfg["base_url"]
        key = os.environ.get("LLM_API_KEY") or (os.environ.get(cfg["key_env"]) if cfg["key_env"] else None)
        if not base_url:
            raise SystemExit("Defina LLM_BASE_URL para o provedor 'custom'.")
        if cfg["key_env"] and not key:
            raise SystemExit(f"Defina {cfg['key_env']} (ou LLM_API_KEY) para usar o provedor '{self.provider}'.")
        self.model = model or os.environ.get("LLM_MODEL") or cfg["model"]
        if not self.model:
            raise SystemExit("Defina LLM_MODEL.")
        self.client = OpenAI(base_url=base_url, api_key=key or "not-needed", timeout=60)
        self.temperature = temperature

    def complete(self, messages, tools=None, tool_choice=None, max_tokens=800, retries=5):
        kwargs = {"model": self.model, "messages": messages, "temperature": self.temperature, "max_tokens": max_tokens}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
        for attempt in range(retries):
            try:
                return self.client.chat.completions.create(**kwargs).choices[0].message
            except openai.BadRequestError as e:
                # Groq devolve 400 'tool_use_failed' quando o modelo gera uma chamada malformada: vale tentar de novo
                if "tool_use_failed" not in str(e) or attempt == retries - 1:
                    raise
                wait = 1
            except RETRYABLE as e:
                if attempt == retries - 1:
                    raise
                wait = 2 ** attempt * 4
                print(f"  [llm] {type(e).__name__}; nova tentativa em {wait}s")
            time.sleep(wait)

    def __repr__(self):
        return f"{self.provider}:{self.model}"
