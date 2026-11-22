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

## `wf top` и источник `serp`

`top` — единственная команда позиций: вход/выход в форме Arsenkin `check-top`, источник `--source serp|arsenkin`
(по умолчанию serp). Отдельной `wf serp` нет — `serp.py` внутренний источник `top`.

## `wf top`

Вход/выход — Arsenkin `check-top`; источник `--source serp|arsenkin` (по умолчанию serp). `collect[query][se]` —
URL в порядке выдачи; `summary.top` — URL по силе. Неподдерживаемый движок (YouTube) даёт пустой список и индекс
в `skipped`, не ошибку. Arsenkin-статусы: известные «готово»/«провал», всё остальное (`starting`, `process`) — ждём.

## `wf clustering`

Вход — Arsenkin `clustering` (обязательные `queries`, `se` 1/2, `region`; `group` soft, `count` 3, `depth` 10,
`stoplist` [], `ws` [], `main` false). Источник `--source serp|arsenkin`. `serp` — свой стек: топы через `serp
search`, группировка по пересечению URL: `hard` — не меньше `count` общих URL и половина меньшего топа, `soft` — один
общий URL и 40% меньшего топа (доля обязательна: популярный агрегатор иначе склеивает всю нишу в один кластер; слияние
жадное, сильнейшее первым), частоты через
`wordstat search` только для типов из `ws`. Выход в их именах полей: `clustered`, `words`, `wssumm`, `topurl`, `main`,
`agregators`/`geo` — `null` (нужны ихние списки). `main: true` убирает главные страницы из группировки, но считает их
в поле `main`. Упавший запрос — в `summary.skipped`, не ошибка всей задачи.

## `wf wordstat`

Arsenkin `wordstat`: `type` 1 — частоты (`queries`, `regions` до 4, `ws`, `device`), `type` 2 — парсинг фраз
(`queries`, `region`, `minus_words`, `is_right`, `is_clear`, `is_clear_minus`). Источник `--source wordstat|arsenkin`.
Наш CLI принимает несколько регионов за вызов (`--lr 213,225`: каждый регион — свой платный запрос, ответ помечен
регионом), `--device` и `--limit` (`groupby` XMLStock) проброшены.
Имена частот:
`base`→`broad`, `quoted`→`phrase`, `overal`→`exact`, `exact`→`exact-ord`. Фразы — это `broad`-ответ CLI: `popular` —
левая колонка, `associations` — правая, минус-слова и «+» фильтруются локально.

## `wf highlights`

Arsenkin `sp`: `queries`, `se` 1/2, `region`, `depth` 10/20/50; источник `--source serp|arsenkin`. Свой источник —
`serp snippets` (поле `highlights` — слова, помеченные провайдером жирным), затем счёт у нас: `highlights` по числу
результатов и `lsi` (слова и пары слов по числу документов, стоп-слова выкинуты). `--with questions,ads,ai` добавляет
`serp related`, `serp ads` и `serp summary`. Arsenkin-ответ кэшируется в общей таблице, повтор — `--cache`.

## `wf task` и слой задач

Медленная работа ставится через `--submit` и читается через `wf task <id> [--wait]`; тип задачи (`kind`) лежит в
записи, наружу не передаётся. Id с префиксом хранилища: `local:…` (файл на машине, воркер `wf task --run <id>`),
`arsenkin:…` (состояние спрашиваем у них). В Python: `top.collect/submit`, `semantics.collect/submit`,
`tasks.get/wait`.

## Слой задач

`tasks.submit(kind, request)` → id; `tasks.get/wait/cancel`; `tasks.gc` убирает завершённые файлы. Id:
`<store>:<kind>:<name>` (`local:…`, `arsenkin:…`), по нему `CompositeStore` выбирает хранилище. Протокол `Store` —
`create/get/save/cancel`, асинхронный: FastAPI/MCP подменит реализацию, остальное не меняется. Отмена локальной задачи
шлёт SIGTERM воркеру, а отменённая задача не перезаписывается поздним результатом.

## `wf` и кэш Arsenkin

`cache.py` хранит payload задачи Arsenkin: ключ — sha256 от `{"kind", "request"}`, таблица
`public.workflows_answers` при `WORKFLOWS_DATABASE_URL`, иначе SQLite в кэше пользователя. `top`/`clustering`/
`wordstat` с `--source arsenkin --cache` читают оттуда, `--max-age` ограничивает возраст. Сбой кэша — промах, не ошибка.

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
