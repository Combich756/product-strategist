# Формат артефактов

Каждый артефакт — JSON-объект UTF-8. Тексты в полях непустые. Примеры по всем этапам лежат в [demo/checkpoints](../demo/checkpoints). Подставляй данные текущего кейса.

| Этап | Поля |
|---|---|
| `brief` | Строки `question`, `goal`, `audience`, `constraints`, `available_data`; список строк `questions`. |
| `research` | Строка `comparison`, список строк `gaps`, список `claims`. |
| `options` | Строка `comparison`, список `options`. |
| `strategy` | Строки `option` (ID варианта), `rationale`, `revisit`; объекты `product`, `communication`, `test`. |
| `decision` | Строки `option` (ID рекомендованного варианта), `approved_by`, `rationale`; список `commitments`. |

## Исследование

`claims[]` содержит `id` (`C1` и далее), `type` (`fact`, `hypothesis`, `forecast`, `unknown`), `scope` (`case`, `public`, `demo`), `text`, `limits`, `refs`.

Ссылка имеет вид `{"source":"S002","start":2,"end":5}`. Нумерация строк с единицы, обе границы включительно. Сохраняй короткий диапазон из активного снимка. Для факта нужна хотя бы одна ссылка. Факт, основанный только на открытых источниках, получает `scope=public`. Любое утверждение со ссылкой на синтетический источник получает `scope=demo` и ограничивается демонстрацией. Для неизвестного можно передать пустой `refs`.

Сохраняй один и тот же ID при пересмотре смысла того же тезиса. Новый тезис получает новый ID. В `limits` указывай применимость и что могло бы изменить вывод.

## Варианты и стратегия

- `options[]` содержит `id` (`O1` и далее), `kind` (один `status_quo`, один `close`, минимум один `change`), `title`, `action`, `benefit`, `objection`, `economics` и список ID `claims`.
- `product` содержит `audience`, `job`, `alternatives`, `direction`, `tradeoffs`, `economics` и список ID `claims`.
- `communication` содержит `audience`, `situation`, `barrier`, `message`, `proof`, `channel_roles`, `promise_limit` и список ID фактов `proof_claims`. Для собственного обещания нужны правила или наблюдение о своём продукте. Факта о конкуренте для этого недостаточно.
- `test` содержит `expected`, `metric`, `denominator`, `control`, `window`, `threshold`, `guardrails` и список ID `claims`. Ожидаемый эффект обозначай отдельно от наблюдаемого результата.

## Решение

`commitments[]` содержит `id` (`K1` и далее), `task`, `owner`, `due` (ГГГГ-ММ-ДД), `metric`, `trigger`. Записывай роль ответственного и только разрешённые идентификаторы. При отсутствии утверждения оставляй кейс на `decision`.

Валидация проверяет структуру, этап, ссылки, диапазоны и зависимости. Качество исследования, верность пересказа и достаточность доказательств проверяются агентом по материалам и человеком при решении.
