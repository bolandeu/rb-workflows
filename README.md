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
