# AGENTS.md — wf

Воркфлоу как код на базе Windmill Workflow-as-Code: `@workflow` + `@task` из `wmill`. Тот же файл гоняется локально
(`@task` выполняет тело, `step()` инлайнится, `sleep()` уходит в `asyncio.sleep`) и деплоится в Windmill без правок.

## Границы

- Шаг (`adapters/`) умеет **только** достать данные: URL → JSON-документ. Ключи и прокси берёт `credentials.binding()`,
  не сценарий.
- Сценарий (`flows/`) не знает про сети и ключи: порядок шагов, условия перехода, бюджет.
- Ничего, кроме JSON, через границу шага: результат `@task` чекпоинтится в Windmill, `datetime` вернётся строкой.
- Секреты не попадают в `step()` — он чекпоинтится. Только внутрь `@task`.

## Провайдеры: порядок и правила

- Первым всегда `spider` (свой стек, бесплатно), затем `exa`; `tavily` и `direct` реализованы, но в водопад по
  умолчанию не входят.
- `ok: false` с `reason` (`blocked`, `empty_text`, `no_key`, `timeout`) — сигнал идти дальше. Исключение в шаге —
  неожиданность: оно не валит воркфлоу, но попадает в `tried[]`.
- `trust_env=False` у всех HTTP-клиентов: `HTTP(S)_PROXY` не включает прокси сам, только `--proxy` или привязка ключа.

## Сервисы семантики

`wf semantics` получает фразы по URL у внешнего сервиса; обёртка (`semantics.py`) держит лимиты (5 задач, 30 запросов
в минуту), пачку и сводку. Сервис — модуль `services/<name>.py` с `async def collect(...)` и строка в `SERVICES`;
новый сервис не трогает обёртку, CLI и форму отчёта. Первый — Arsenkin: `set` → `check` → `get`, `mode=url`.

## `wf serp`

Пачка запросов в `serp search` параллельно (`--concurrency`), все параметры CLI наружу. `summary` считает силу URL:
`Σ (top - pos + 1)/top` по попаданиям, делённое на число запросов × ПС; топ-10. Упавший запрос — запись с
`ok: false`, пачка продолжается. Python: `await rb_workflows.serp.collect(...)`.

## `wf top`

Вход/выход — Arsenkin `check-top`; источник `--source serp|arsenkin` (по умолчанию serp). `collect[query][se]` —
URL в порядке выдачи; `summary.top` — URL по силе. Неподдерживаемый движок (YouTube) даёт пустой список и индекс
в `skipped`, не ошибку. Arsenkin-статусы: известные «готово»/«провал», всё остальное (`starting`, `process`) — ждём.

## Ключи

`credentials.binding(service)` выбирает бэкенд по месту запуска: Windmill-ресурс (`WM_TOKEN`) → 9router
(`RB_WORKFLOWS_SOURCE=9router`, `RB_WORKFLOWS_KEY=<имя>`) → локальный `rb_credentials.Store("workflows")`
(`WORKFLOWS_<SERVICE>_API_KEY`, `WORKFLOWS_<SERVICE>_PROXY`). `rb-9router` — dev/path, никогда в `dependencies`.

## Проверки

```sh
uv sync
uv run pytest -q
uv run ruff check . && uv run ty check
wf run web-fetch --urls https://example.com --json
```
