# wf

Воркфлоу как код на базе [Windmill Workflow-as-Code](https://github.com/windmill-labs/windmill): `@workflow`,
`@task`, `step()`. Тот же файл гоняется локально и деплоится в Windmill без правок — вне джоба `@task` просто
выполняет тело, `step()` выполняется инлайн, `sleep()` падает в `asyncio.sleep`. Шаги (`adapters/`, источники
`web_search`) помечены `@task`: в Windmill каждый — отдельный джоб с чекпоинтом, retry и таймаутом, поэтому реплей не
платит за уже собранное.

Два воркфлоу: **`web-fetch`** — безотказно достать страницу (HTML, текст, метатеги), и **`web-search`** — собрать
информацию по теме из нескольких поисковых источников. Каждый воркфлоу — своя команда (`wf web-fetch`,
`wf web-search`); `wf run <workflow> --input '{…}'` — общий машинный вход для любого из них.

## Запуск

```bash
wf web-fetch --urls https://example.com --json
wf web-fetch --input '{"urls": ["https://a", "https://b"], "budget_usd": 0.01}' --json
wf web-fetch --urls https://example.com --steps exa --proxy socks5://u:p@h:1080
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

## `web-search` — информация по теме, из нескольких источников

Задача другая, чем у `web-fetch`: не достать страницу безотказно, а собрать материал. Источники: свой `serp`
(настоящая выдача Yandex/Google с позициями и сниппетами, аккаунты XMLRiver/XMLStock у самого CLI), `exa` и `tavily`
(их подбор и готовый ответ модели).

```bash
wf web-search --queries "требования к метатегу title" --json              # каскад, serp первым
wf web-search --queries "..." --mode parallel --sources serp,exa --json   # все сразу, сведение по URL
wf web-search --queries "..." --fetch-top 3 --json                        # плюс текст трёх верхних страниц
```

| шаг | что даёт | цена |
| --- | --- | --- |
| `serp` | `serp snippets --json`: позиции, сниппеты, подсветки | рубли, считает CLI (`cost`, `cost_currency`) |
| `exa` | `POST /search`: их подбор | `costDollars.total`, ~$0.005/запрос |
| `tavily` | `POST /search`: подбор плюс `answer` | кредиты, в `cost_usd` пересчитаны |

- `--mode cascade` (по умолчанию) — по порядку `serp` → `exa` → `tavily`, первый ответивший закрывает запрос.
- `--mode parallel` — все выбранные сразу; сведение по URL: `sources`, `positions`, `best_position`, сырые списки
  остаются в `by_source`.
- `--fetch-top N` — текст верхних N страниц: HTML от `spider` (затем прямой GET), текст через trafilatura (главный
  контент без меню и футера); пустое извлечение — markdown от `spider`.
- Источник без ключа или упавший — причина в `tried`, прогон не падает.

Ключи — `credentials.binding()`: локально 9router (`RB_WORKFLOWS_SOURCE=9router`: несколько аккаунтов на сервис и
прокси каждого), в Windmill — ресурс. У `serp` ключей воркфлоу нет — CLI берёт XMLRiver/XMLStock сам.

```json
{"ok": true, "mode": "parallel", "sources": ["serp", "exa"], "budget_spent_usd": 0.005,
 "queries": [{"query": "...", "ok": true, "source": "serp",
              "results": [{"position": 1, "url": "...", "domain": "...", "title": "...", "snippet": "...",
                           "sources": ["serp", "exa"], "positions": {"serp": 1, "exa": 3}, "best_position": 1}],
              "answer": null, "tried": [], "by_source": {"serp": {"ok": true, "results": []}},
              "documents": [{"url": "...", "ok": true, "text": "...", "source": "spider", "via": "trafilatura"}]}]}
```

## `semantics` — ключевые фразы по URL

Обёртка над внешними сервисами, которые собирают семантику по URL. Первый сервис — Arsenkin TOOLS
(`tools_name=semantics`, `mode=url`, регионы `msk` — Яндекс Москва и `gru` — Google Москва).

```bash
wf semantics --urls https://site.ru/page --json
wf semantics --input semantics.json --json                 # тело Arsenkin как есть (mode, queries, region, stopwords)
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

### источник `serp`

Отдельной команды `wf serp` нет: `serp.py` — это внутренний источник для `wf top` (`--source serp`, по умолчанию).
Он зовёт `serp search` по одному вызову на запрос, параллельно до `--concurrency`, и складывает ответы в общий отчёт
`top`. Пачка запросов, кэш журнала и все параметры `serp` приходят через поле `queries` и `--concurrency`.

## `task` — поставил и вернулся

Любая медленная команда ставится сразу и читается потом; тип задачи передавать не нужно, он записан при постановке.
Идентификатор говорит, где живёт состояние: `local:20261121-101500-ab12cd34` — файл на этой машине,
`arsenkin:32070249` — задача у Arsenkin.

```bash
wf semantics --urls https://a.ru https://b.ru --submit --json   # {"task_id": "local:…", "poll": "wf task local:… --json"}
wf top --input top.json --source arsenkin --submit --json       # {"task_id": "arsenkin:32070249", …}
wf task local:20261121-101500-ab12cd34 --json                   # состояние или готовый отчёт
wf task arsenkin:32070249 --wait --json                         # блокировать до конца
wf task local:… --cancel                                        # остановить задачу и её воркер
wf task --gc --older-than 7 --json                              # убрать завершённые старше недели
```

Задачи хранятся в `Store` (сейчас два: файлы на этой машине и сам Arsenkin), снаружи это четыре вызова —
`create`, `get`, `save`, `cancel`. Приложение на FastAPI подставит свою реализацию, команды и формат ответа не
изменятся.

## `top` — выгрузка ТОП, из двух источников

Вход и выход — формат `check-top`: задание в этом формате работает здесь без правок. Источник выбирается: наш `serp`
(по умолчанию) или Arsenkin (`--source arsenkin`).

```bash
wf top --queries "ремонт киа" --se 1:213 --json          # то же, но флагами
wf top --input top.json --source serp --json            # наш serp search
wf top --queries "q" --provider xmlriver,xmlstock --cache --max-age 7d --json
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

## `clustering` — кластеры запросов, из двух источников

Вход и выход — формат `clustering`. Источник — свой стек (`serp` + `wordstat`, по умолчанию) или Arsenkin
(`--source arsenkin`).

```bash
wf clustering --queries "ремонт киа" "сервис киа" --se 1 --region 213 --json
wf clustering --queries "q" --se 1 --region 213 --group hard --count 3 --ws base,exact --json
wf clustering --input clustering.json --stoplist auto.ru,yandex.ru --main --json
wf clustering --input clustering.json --source arsenkin --json
```

```json
{"tools_name": "clustering",
 "data": {"queries": ["ремонт киа", "сервис киа"], "se": 1, "region": 213, "group": "soft", "count": 3,
          "depth": 10, "stoplist": ["auto.ru"], "ws": ["base", "exact"], "main": true}}
```

Свой источник: топы по каждой фразе через `serp search`, группировка по пересечению URL. `hard` склеивает группы с не
меньше `count` общими URL и не меньше половины меньшего топа, `soft` — с хотя бы одним общим URL и 40% меньшего топа.
Доля обязательна: без неё URL-агрегатор, стоящий в каждом топе ниши, склеивает всё в один кластер. Слияние жадное,
сильнейшее пересечение первым, URL сравниваются в каноническом виде (`http/https`, `www.`/`m.`, хвостовой слэш,
`index.html`, utm-метки не различаются). `stoplist` выкидывает домены, `main` — главные страницы. Пересборка с другим
`count` бесплатна: `--cache` берёт топы из журнала `serp`.
Частоты берутся у `wordstat search` одним вызовом и только для типов из `ws` (`base` → `broad`, `quoted` → `phrase`,
`exact` → `exact`, `overal` = `base`).

Ответ: `result.request` — эхо входа, `result.result` — кластеры в их именах полей: `clustered` (имя кластера — сильнейшая
фраза), `words`, `wssumm` (сумма частот), `topurl` (URL-лидер), `main` (сколько главных страниц в его топах),
`queries` (по фразе: `ws`, `ws_strict`, `ws_types`). `agregators` и `geo` у своего источника `null`: для них нужны их
списки. `summary` — число кластеров, фраз и `skipped` — запросы, которые источник не ответил.

## `wordstat` — частотность и парсинг фраз, из двух источников

Вход и выход — формат `wordstat` (один `tools_name`, два `type`). Источник — наш CLI `wordstat` (по умолчанию) или
Arsenkin (`--source arsenkin`).

```bash
wf wordstat --queries "ремонт киа" --regions 213,225 --ws base,exact --json          # type 1: частоты
wf wordstat --type 2 --queries "ремонт киа" --region 213 --minus-words дилер --json  # type 2: фразы
wf wordstat --input wordstat.json --source arsenkin --json
```

```json
{"tools_name": "wordstat",
 "data": {"type": 1, "queries": ["ремонт киа"], "regions": [213, 225], "ws": ["base", "exact"], "device": ""}}
```

Ответ в их раскладке: type 1 — `result.data.result[фраза][регион][вид]`; type 2 — `result.data[]` по фразе с `freq`,
`left` (похожие запросы) и `right` (ассоциации). Наша `summary` считает строки.

Имена частот переводятся на язык CLI: `base` → `broad`, `quoted` → `phrase`, `overal` → `exact` (`!фраза`),
`exact` → `exact-ord` (`[!фраза]`). `--device desktop|phone|tablet` уходит в CLI (частота реально другая: «таможенное
оформление» — 9371 на всех устройствах и 5083 на десктопе), `--limit` (1..2000) — это `groupby` XMLStock, у XML River
глубина задаётся в аккаунте. Регионов в `regions` может быть до 4, как в контракте: свой движок спрашивает их одним вызовом CLI (каждый регион —
отдельный платный запрос), Arsenkin получает их списком. Регион `0` (вся страна) спрашивается как `225`.

## `highlights` — подсветки и LSI, из двух источников

Вход — формат `sp`: `queries`, `se` (1 Яндекс, 2 Google), `region`, `depth` (10/20/50). Источник — наш `serp`
(по умолчанию) или Arsenkin (`--source arsenkin`). Что мы добавляем к контракту: `--with questions,ads,ai` — вопросы (`serp related`),
реклама (`serp ads`) и сводка (`serp summary`) в том же прогоне.

```bash
wf highlights --queries "купить диван" --se 2 --region 1011969 --with questions --json
wf highlights --input sp.json --source arsenkin --cache --json
```

Ответ по каждой фразе: `results` (позиция, URL, заголовок, сниппет и **подсветки**), `highlights` (каждое выделенное
жирным словосочетание и сколько результатов его несут) и `lsi` (слова и пары слов из заголовков и сниппетов по числу
результатов, где они есть — тема, а не запрос). `summary` считает результаты, строки с подсветками и число терминов.

Подсветки берутся из разметки провайдера (`<hlword>`, у XML River — `<b>`): `serp snippets` с версии 0.8.7 отдаёт
поле `highlights`. Ответ Arsenkin кэшируется в общей базе (`--cache`, `--max-age`), наши вызовы — в журнале `serp`.

## Кэш ответов Arsenkin

Ответ их задачи (`get`) сохраняется как есть и читается повторно бесплатно: ключ — инструмент и тело запроса,
значение — их payload. `--cache` берёт сохранённый ответ, `--max-age 7d` отказывает слишком старому.

- `WORKFLOWS_DATABASE_URL` задан → таблица `public.workflows_answers` в общей PostgreSQL (создаётся сама);
- не задан → локальный SQLite `~/Library/Caches/rb-workflows/answers.sqlite`.

Работает для `top`, `clustering`, `wordstat` при `--source arsenkin`.

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
