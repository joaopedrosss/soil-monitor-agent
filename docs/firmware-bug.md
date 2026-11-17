# Bug no firmware: a irrigação nunca liga

**Repositório:** `soil-monitor-esp32-firmware` · **Patch:** [`fix-static-shared-state.patch`](fix-static-shared-state.patch)

## Sintoma

O LED de irrigação nunca acende, mesmo com o potenciômetro de umidade no mínimo, e o
dashboard mostra sempre "não irrigando".

## Causa

`main.c` e `led.c` declaram, **cada um**, as variáveis compartilhadas entre as threads:

```c
// main.c                                      // led.c
static volatile bool esta_irrigando = false;   static volatile bool esta_irrigando = false;
static volatile uint16_t ultima_umidade = 2000; static volatile uint16_t ultima_umidade = 2000;
```

Em C, `static` no escopo de arquivo significa *linkage interna*: cada arquivo `.c` ganha a
**sua própria cópia** da variável. São compilados separadamente (CMakeLists lista os dois), então:

| Thread | Escreve | Lê | Efeito |
|---|---|---|---|
| `main()` (main.c) | sua `ultima_umidade` | sua `esta_irrigando` (sempre `false`) | envia `irrigando: false` sempre |
| `led_thread_function()` (led.c) | sua `esta_irrigando` | sua `ultima_umidade` (sempre 2000) | `2000 < 1600` nunca é verdade |

O compilador não avisa: as duas declarações são válidas isoladamente.

## Correção

Definir cada variável **uma vez** (em `main.c`, sem `static`) e declará-la como `extern` em
`led.h`, que os dois arquivos incluem. O patch muda 3 arquivos (+13/−15 linhas) e aplica
limpo com `git apply docs/fix-static-shared-state.patch`.

Demonstração mínima do comportamento (gcc, fora do Zephyr), com a umidade em 1500:

```
com static em cada arquivo:  ultima_umidade=1500 -> esta_irrigando=0
com extern compartilhado:    ultima_umidade=1500 -> esta_irrigando=1
```

## Observações

- `volatile` impede que o compilador guarde o valor em registrador, mas **não** garante
  atomicidade. Aqui é suficiente: no ESP32 (Xtensa 32 bits), leituras e escritas de `bool` e
  `uint16_t` alinhados são atômicas, e só há um escritor por variável. Para estado mais
  complexo, use `atomic_t` (API `atomic_*` do Zephyr) ou publique pelo próprio zbus, que o
  projeto já usa.
- Melhoria sugerida no servidor Flask: guardar o horário em que cada leitura chegou
  (`recebido_em`) e devolvê-lo no `GET /api/dados`. Hoje, se o ESP32 parar, o servidor
  continua servindo o último valor indefinidamente, e clientes não têm como saber.
