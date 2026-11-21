# wf

Воркфлоу как код на базе [Windmill Workflow-as-Code](https://github.com/windmill-labs/windmill): `@workflow`,
`@task`, `step()`. Тот же файл гоняется локально и деплоится в Windmill без правок — вне джоба `@task` просто
выполняет тело, `step()` выполняется инлайн, `sleep()` падает в `asyncio.sleep`.

Первый воркфлоу — **`web-fetch`**: один URL или список. Первым идёт свой `spider fetch` (бесплатно, со своим
фингерпринтом и прокси), и только если он не вернул текст — водопад по платным сервисам (пока exa).

## Запуск

```bash
wf run web-fetch --urls https://example.com --json
wf run web-fetch --input '{"urls": ["https://a", "https://b"], "budget_usd": 0.01}' --json
wf run web-fetch --urls https://example.com --steps exa --proxy socks5://u:p@h:1080
```

`--json` — контракт (его читают скрипты и агенты), без него — короткая сводка. Exit 0 — хоть что-то получено.

## Водопад

| шаг | что делает | цена |
| --- | --- | --- |
| `spider` | `spider fetch --json`: свой стек — фингерпринт, прокси, браузер. Первый, всегда | 0 |
| `exa` | `POST /contents`: лучший текст для LLM. Только если spider не дал текста | `costDollars.total`, ~$0.001/страница |

`tavily` (`/extract`) и `direct` (httpx `GET`) реализованы и покрыты тестами, но в водопад по умолчанию не входят:
включаются строкой в `ADAPTERS` или через `--steps`.

Шаг возвращает `ok: false` с причиной (`blocked`, `empty_text`, `http:404`, `no_key`, `timeout`) — это и есть
сигнал идти дальше. Исключение — только для неожиданного, его ретраит Windmill.

## Формат

Вход — сигнатура функции (`urls: list[str]`, `budget_usd`, `proxy`, `steps`); в Windmill из неё строится форма.
Выход — JSON:

```json
{"ok": true, "budget_spent_usd": 0.001,
 "documents": [{"url": "...", "final_url": "...", "ok": true, "status": 200, "title": "...",
                "text": "...", "text_sha256": "...", "text_truncated": false,
                "source": "exa", "via": "proxy", "cost_usd": 0.001, "reason": null,
                "tried": [{"step": "exa", "reason": "blocked"}]}]}
```

Результат шага чекпоинтится в Windmill, поэтому документ — всегда JSON и текст усечён до 200 000 символов.

## `semantics` — ключевые фразы по URL

Обёртка над внешними сервисами, которые собирают семантику по URL. Первый сервис — Arsenkin TOOLS
(`tools_name=semantics`, `mode=url`, регионы `msk` — Яндекс Москва и `gru` — Google Москва).

```bash
wf semantics --urls https://site.ru/page --json
wf semantics --urls https://site.ru/a https://site.ru/b --stopwords "купить,цена" --json
wf semantics --urls https://site.ru/page --engine gru --service arsenkin --json
```

Задача в Arsenkin асинхронная: `set` → `task_id`, опрос `check`, затем `get`. Лимиты аккаунта, которые обёртка
держит сама: не больше 5 задач одновременно (`--concurrency`) и 30 запросов в минуту на все статусы. Пачка не ждёт
первый URL: до пяти задач идут параллельно, упавшая страница не останавливает остальные.

Ответ: по каждому URL — свой список фраз с позицией и флагами (`ws`, `ws_absent` — «[!WS]», `toponym`, `question`),
плюс `summary` по всем URL: фраза, число URL, где она встретилась, и позиция в каждом. Фраза без позиции остаётся в
сводной с `position: null`.

**Ещё сервис** — это `src/rb_workflows/services/<name>.py` с `async def collect(...)` плюс строка в `SERVICES`.
Обёртка, CLI и формат отчёта не меняются.

## `serp` — позиции по пачке запросов и сила кластера

Обёртка над CLI `serp`: любое число запросов, каждый — свой вызов `serp search`, параллельно до `--concurrency`.
Параметры CLI пробрасываются как есть.

```bash
wf serp --queries "купить диван" "доставка воды офис" --engine both --top 20 --json
wf serp --queries "seo" --provider xmlriver,xmlstock --cache --max-age 7d --yandex-lr 213 --json
```

Ответ: `results[]` — по записи на (запрос × ПС) с позициями, `sources[]` и стоимость как отдал `serp`, и `summary` —
топ-10 URL по силе кластера:

```
strength(url) = Σ (top - позиция + 1) / top  по всем попаданиям
                ─────────────────────────────
                число запросов × число ПС
```

1.0 — первый в каждой ПС по каждому запросу. В строке сводки: `queries_count` (в скольких запросах встречается),
`appearances` (всего попаданий), `best_position`, `by_query` (позиция в каждой ПС по каждому запросу, `null` если нет).

## `top` — выгрузка ТОП в формате Arsenkin, из двух источников

Вход и выход — формат Arsenkin `check-top`, поэтому задание, написанное под их API, работает здесь без правок,
а отчёт читается тем, что читает их ответ. Источник выбирается: наш `serp` (по умолчанию) или сам Arsenkin.

```bash
wf top --input top.json --source serp --json          # наш serp search
wf top --input top.json --source arsenkin --json      # их задача check-top
```

```json
{"tools_name": "check-top",
 "data": {"queries": ["ремонт киа"], "se": [{"type": 1, "region": 213}, {"type": 11, "region": 1011969}],
          "depth": 10, "is_snippet": true, "noreask": false}}
```

Ответ: `result.request` (эхо, `se` → `ss`), `result.result.collect[запрос][se]` — URL в порядке выдачи, плюс наша
сводка `summary`: `urls_total`, `top_n` и `top` — URL по силе (`(depth - позиция + 1)/depth`, нормировано на число
запросов × ПС), с `queries_count`, `appearances`, `best_position`, `by_query`.

Движок, который источник не умеет (YouTube, коды 20/21), — не ошибка: список пуст, а его индекс назван в `skipped`.

## Ключи и прокси

`credentials.binding(service)` выбирает бэкенд по месту запуска, а не по воркфлоу:

- в джобе Windmill (`WM_TOKEN`) — `wmill.get_resource("u/me/<service>")`, затем `f/rb_workflows/<service>`;
- `RB_WORKFLOWS_SOURCE=9router` (`RB_WORKFLOWS_KEY=<имя>` для конкретного подключения) — ключи, прокси и
  схема ротации из локальной базы 9router (`rb-9router`, приватный пакет);
- иначе — `rb_credentials.Store("workflows")`: `WORKFLOWS_<SERVICE>_API_KEY`, `WORKFLOWS_<SERVICE>_PROXY`.

Явный `--proxy` перебивает привязку ключа. Секреты никогда не попадают в `step()` (он чекпоинтится) — только
внутрь `@task`.

## Разработка

```sh
uv sync
uv run pytest -q
uv run ruff check . && uv run ty check
```
