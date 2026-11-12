# Проверка материалов Android W42

Задание: `android-w42`, кейс `worker-live-a0cbe9a1`, входная ревизия **8**.
Результат: **partial** — арифметика агрегата подтверждена; независимый событийный журнал недоступен.

## Область доступа и состояние

Прочитаны инструкции `.qwen/agents/materials-researcher.md`, `agent/SUBAGENTS.md`, разрешённый главным агентом контракт `agent/SCHEMA.md` и `task.json` этой подзадачи. Источники фактов — только четыре снимка из manifest. Команда `status` показала revision=8, paused=false, next=research, active_sources=S001,S002,S003,S004. `sources` подтвердила те же активные источники.

Рабочий каталог всех shell-команд:
`/workspace/scratch/1c0c97432bf8/product-strategist`.

## Фактически выполненные команды поиска и расчётов

```bash
python3 -m strategist status worker-live-a0cbe9a1
python3 -m strategist sources worker-live-a0cbe9a1
python3 -m strategist find worker-live-a0cbe9a1 'Android'
python3 -m strategist rates worker-live-a0cbe9a1 S002 --by week platform --numerator connected_48h --denominator eligible_orders --evidence
python3 -m strategist rates worker-live-a0cbe9a1 S002 --by week --numerator connected_48h --denominator eligible_orders --evidence
python3 -m strategist find worker-live-a0cbe9a1 'журнал'
python3 -m strategist find worker-live-a0cbe9a1 'сесс'
```

Поиск `Android` вернул S002:3, S002:5 и S003:5. Поиски `журнал` и `сесс` вернули пустые списки. Пустой результат поиска сам по себе не используется как доказательство отсутствия журнала: все четыре разрешённых снимка прочитаны полностью.

Для чтения и проверки целостности снимков выполнен следующий Python-код через `python3 - <<'PY'`:

```python
from pathlib import Path
import hashlib
base = Path('cases/worker-live-a0cbe9a1/workers/android-w42/snapshots')
for name in ['S001.md', 'S002.csv', 'S003.md', 'S004.md']:
    p = base / name
    raw = p.read_bytes()
    print(f'{name}: sha256={hashlib.sha256(raw).hexdigest()}')
    for number, line in enumerate(raw.decode('utf-8').splitlines(), 1):
        print(f'{number}: {line}')
```

Хеши фактически прочитанных байтов совпали с `text_sha256` в задании:

| Источник | Снимок | SHA-256 |
|---|---|---|
| S001 | snapshots/S001.md | 6f1896a0bb5892d9799aa1696bfcda5d4e730eaf59b0653022f76608b107500a |
| S002 | snapshots/S002.csv | e9f1ef0fc718f3b781ff44efb419cd15fa6b2fce08276014785cf30a30356ce7 |
| S003 | snapshots/S003.md | bacaec9a5437746add2838cf22dfe2b404ba839f18603dfc374dcc5bd9520320 |
| S004 | snapshots/S004.md | b4965416615de94171edb761a3184da990d75e5cc359252864eeaf9c0f8225bc |

## Определения и точные фрагменты

S001:2 прямо обозначает пилот вымышленным, а показатели и процессы — смоделированными.

S001:5:
> В выгрузке за W41 и W42 единица — оплаченный совместимый заказ с известной датой прибытия, для которого прошли 48 часов после прибытия. `connected_48h` — заказы с первой сессией мобильных данных по eSIM в течение этих 48 часов. Срезы по платформе и неделе не пересекаются.

Это окно после прибытия, а не после покупки или установки профиля. Зрелость когорты заявлена в паспорте, но не проверяема по индивидуальным событиям.

S002:1:
`week,platform,eligible_orders,connected_48h`

S002:2–5 содержит четыре агрегированные строки, без ключей заказов и времён событий:

```csv
2026-W41,iOS,300,210
2026-W41,Android,100,70
2026-W42,iOS,180,126
2026-W42,Android,220,110
```

S003:1–5 — локальный конспект справки Apple о требованиях и вариантах использования eSIM на iPhone; в нём прямо ограничена применимость к Android и данным покупателей. S004:1–5 — конспект правил Airalo о моменте запуска срока пакета. Ни один конспект не содержит наблюдений о событиях этого пилота.

## Расчёты

Доля = `sum(connected_48h) / sum(eligible_orders)`; разность долей выражена в процентных пунктах.

| Период | Платформа | connected_48h | eligible_orders | Доля | Строка S002 |
|---|---|---:|---:|---:|---:|
| 2026-W41 | Android | 70 | 100 | 70% | 3 |
| 2026-W42 | Android | 110 | 220 | 50% | 5 |
| 2026-W41 | iOS | 210 | 300 | 70% | 2 |
| 2026-W42 | iOS | 126 | 180 | 70% | 4 |
| 2026-W41 | Все платформы | 280 | 400 | 70% | 2–3 |
| 2026-W42 | Все платформы | 236 | 400 | 59% | 4–5 |

- Android: `(110/220 - 70/100) × 100 = -20` п.п.
- iOS: `(126/180 - 210/300) × 100 = 0` п.п.
- Все платформы: `((126+110)/(180+220) - (210+70)/(300+100)) × 100 = -11` п.п.
- Число подключившихся Android-заказов: `110 - 70 = +40`; поэтому формулировка «снизилось число первых подключений Android» не подтверждается, а снижение доли подтверждается.

Готовые доказательства из CLI, без ручного изменения счётчиков или номеров строк:

```json
{
  "by_week_platform": {
    "source": "S002",
    "refs": [
      {
        "source": "S002",
        "start": 2,
        "end": 5
      }
    ],
    "rate_checks": [
      {
        "source": "S002",
        "group": {
          "week": "2026-W41",
          "platform": "Android"
        },
        "numerator": "connected_48h",
        "denominator": "eligible_orders",
        "expected": {
          "numerator": 70,
          "denominator": 100
        }
      },
      {
        "source": "S002",
        "group": {
          "week": "2026-W41",
          "platform": "iOS"
        },
        "numerator": "connected_48h",
        "denominator": "eligible_orders",
        "expected": {
          "numerator": 210,
          "denominator": 300
        }
      },
      {
        "source": "S002",
        "group": {
          "week": "2026-W42",
          "platform": "Android"
        },
        "numerator": "connected_48h",
        "denominator": "eligible_orders",
        "expected": {
          "numerator": 110,
          "denominator": 220
        }
      },
      {
        "source": "S002",
        "group": {
          "week": "2026-W42",
          "platform": "iOS"
        },
        "numerator": "connected_48h",
        "denominator": "eligible_orders",
        "expected": {
          "numerator": 126,
          "denominator": 180
        }
      }
    ]
  },
  "by_week": {
    "source": "S002",
    "refs": [
      {
        "source": "S002",
        "start": 2,
        "end": 5
      }
    ],
    "rate_checks": [
      {
        "source": "S002",
        "group": {
          "week": "2026-W41"
        },
        "numerator": "connected_48h",
        "denominator": "eligible_orders",
        "expected": {
          "numerator": 280,
          "denominator": 400
        }
      },
      {
        "source": "S002",
        "group": {
          "week": "2026-W42"
        },
        "numerator": "connected_48h",
        "denominator": "eligible_orders",
        "expected": {
          "numerator": 236,
          "denominator": 400
        }
      }
    ]
  }
}
```

В `result.json` для числовых табличных утверждений использованы полученные `refs` и соответствующие `rate_checks`. `cell_checks` не требуются: каждое использованное табличное значение входит в проверки долей.

## Независимая сверка и ограничения

Вся разрешённая область содержит паспорт, одну агрегированную выгрузку и два справочных конспекта. Независимого журнала первых сессий, идентификаторов событий, индивидуальных дат прибытия и времён сессий здесь нет. Повторный расчёт S002 и её недельный итог — проверки одной выгрузки, а не независимое подтверждение тех же событий.

Нужен отдельно зарегистрированный независимый журнал за те же когорты с ключом заказа/профиля, платформой, временем прибытия и первой сессии, часовым поясом, границами полноты и дедупликацией. Без него нельзя установить первопричину различия или проверить соответствие агрегата исходным событиям. Отсутствие журнала ограничено доступными снимками и не означает, что его нет у владельца данных.

Сеть не использовалась (`allowed_urls=[]`). Другие кейсы, готовые ответы, история, исправленные выгрузки и baseline не читались. Команды изменения кейса не вызывались; состояние и материалы не редактировались. Запись ограничена `evidence.md` и `result.json` этой подзадачи.
