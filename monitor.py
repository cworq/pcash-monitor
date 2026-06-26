#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Мониторинг поступлений USDCASH на аккаунт 4store.pcash (сеть Vaulta / ex-EOS).

Что делает скрипт:
  1. Запрашивает у публичной Hyperion History API ноды все действия
     transfer контракта token.pcash, где получатель (to) = ACCOUNT,
     за последние 7 дней (учитывая пагинацию, лимит API — 100 записей за раз).
  2. Оставляет только переводы в токене USDCASH.
  3. Отбрасывает суммы, которые попадают вне диапазона (10, 60) — строго
     больше 10 и строго меньше 60 (границы 10 и 60 исключаются).
  4. Отбрасывает "круглые" суммы — те, что кратны 0.5 (т.е. дробная часть
     .0000 или .5000, например 20.0000 или 20.5000). Остаются только суммы
     с "разнообразным" хвостом, например 24.9740.
  5. Группирует переводы по адресу отправителя (`from`).
  6. Из каждого уникального адреса оставляет МАКСИМУМ 2 перевода
     (самые ранние по времени), остальные дубли с того же адреса отбрасывает.
  7. Сортирует итоговый список по сумме перевода — по убыванию.
  8. Генерирует HTML-страницу с результатом и сохраняет её на диск.

Запуск:
    python3 monitor.py

Можно поставить в cron, например каждый час:
    0 * * * * cd /path/to/pcash_monitor && /usr/bin/python3 monitor.py >> monitor.log 2>&1

Зависимостей кроме стандартной библиотеки Python нет (urllib используется
вместо requests, чтобы скрипт работал "из коробки" без pip install).
"""

import json
import os
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone
from html import escape

# ============================== НАСТРОЙКИ ==============================

ACCOUNT = "4store.pcash"        # чей аккаунт мониторим (получатель)
TOKEN_CONTRACT = "token.pcash"  # контракт-эмитент токена USDCASH
TOKEN_SYMBOL = "USDCASH"        # какой токен нас интересует
DAYS_BACK = 7                   # глубина "последней недели" в днях
MAX_PER_ADDRESS = 2             # максимум переводов с одного адреса в выводе

MIN_AMOUNT = 10                 # нижняя граница суммы (строго больше)
MAX_AMOUNT = 60                 # верхняя граница суммы (строго меньше)
ROUND_STEP = 0.5                # суммы, кратные этому шагу, считаются "круглыми" и отбрасываются
ROUND_EPSILON = 1e-6            # допуск на погрешность float-сравнения при проверке кратности

TOP_GROUP_SIZE = 24       # сколько самых крупных переводов попадает в группу "Капитаны"
SECOND_GROUP_SIZE = 120   # сколько следующих по размеру переводов попадает в "Участники"

# Публичные Hyperion-ноды. Первая в списке — нода от paycash (сервиса самого токена USDCASH),
# остальные — общие публичные ноды EOS-сети как запасной вариант.
HYPERION_ENDPOINTS = [
    "https://hyperion.paycash.online",
    "https://eos.hyperion.eosrio.io",
    "https://eos.eosusa.io",
]

OUTPUT_HTML = "public/index.html"  # Vercel раздаёт статику из папки public/
PAGE_LIMIT = 100   # сколько записей просить за один запрос (максимум у Hyperion обычно 100)
HTTP_TIMEOUT = 15  # секунд на один HTTP-запрос, прежде чем считать ноду недоступной

# ============================== ВСПОМОГАТЕЛЬНОЕ ==============================


def http_get_json(url, timeout=HTTP_TIMEOUT):
    """Простой GET-запрос с разбором JSON-ответа."""
    req = urllib.request.Request(url, headers={"User-Agent": "pcash-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def fetch_actions(endpoint, account, contract, action_name, after_iso, before_iso):
    """
    Тянет ВСЕ действия (с пагинацией) с заданными фильтрами через Hyperion v2 API.
    Возвращает список "сырых" объектов action из ответа Hyperion.
    Печатает прогресс по страницам, чтобы было видно, что скрипт не завис,
    а просто перебирает большую историю.
    """
    all_actions = []
    skip = 0
    page = 1

    while True:
        params = {
            "account": account,
            "filter": f"{contract}:{action_name}",
            "after": after_iso,
            "before": before_iso,
            "limit": PAGE_LIMIT,
            "skip": skip,
            "sort": "desc",
        }
        url = f"{endpoint}/v2/history/get_actions?{urllib.parse.urlencode(params)}"
        print(f"    [.] страница {page} (skip={skip})...", flush=True)
        data = http_get_json(url)

        actions = data.get("actions", [])
        if not actions:
            break

        all_actions.extend(actions)
        print(f"        получено {len(actions)} записей (всего пока: {len(all_actions)})", flush=True)

        # Если получили меньше страницы — значит, это последняя страница.
        if len(actions) < PAGE_LIMIT:
            break

        skip += PAGE_LIMIT
        page += 1

        # защитный предохранитель от бесконечного цикла на случай странного API
        if skip > 20000:
            break

    return all_actions


def parse_transfer(action):
    """
    Достаёт из сырого action нужные поля: from, to, quantity (число), symbol, memo, время, tx id.
    Возвращает None, если структура не похожа на обычный transfer.
    """
    act = action.get("act", {})
    data = act.get("data", {})

    frm = data.get("from")
    to = data.get("to")
    quantity_raw = data.get("quantity")  # обычно строка вида "123.4567 USDCASH"
    memo = data.get("memo", "")

    if not frm or not to or not quantity_raw:
        return None

    parts = str(quantity_raw).strip().split(" ")
    if len(parts) != 2:
        return None

    amount_str, symbol = parts
    try:
        amount = float(amount_str)
    except ValueError:
        return None

    timestamp = action.get("@timestamp") or action.get("timestamp")
    trx_id = action.get("trx_id", "")

    return {
        "from": frm,
        "to": to,
        "amount": amount,
        "symbol": symbol,
        "memo": memo,
        "timestamp": timestamp,
        "trx_id": trx_id,
    }


def is_round_amount(amount):
    """
    Возвращает True, если сумма "круглая" — то есть кратна ROUND_STEP (0.5).
    Примеры круглых сумм при ROUND_STEP=0.5: 20.0000, 20.5000, 35.0000.
    Пример НЕ круглой суммы: 24.9740 (она нам и нужна).
    """
    remainder = round(amount / ROUND_STEP) * ROUND_STEP
    return abs(amount - remainder) < ROUND_EPSILON


def try_endpoints(account, contract, action_name, after_iso, before_iso):
    """Пробует по очереди публичные ноды, пока одна не ответит без ошибки."""
    last_error = None
    for endpoint in HYPERION_ENDPOINTS:
        try:
            actions = fetch_actions(endpoint, account, contract, action_name, after_iso, before_iso)
            return actions, endpoint, None
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
            last_error = f"{endpoint}: {e}"
            continue
    return [], None, last_error


def render_table(rows, empty_message):
    """
    Рендерит одну HTML-таблицу со сквозной нумерацией строк слева (#1, #2, ...).
    rows — список словарей-переводов в нужном порядке.
    """
    if not rows:
        return f'<tr><td colspan="6" class="empty">{escape(empty_message)}</td></tr>'

    table_rows = ""
    for i, r in enumerate(rows, start=1):
        dup_badge = (
            f'<span class="badge">#{r["seq_for_address"]}</span>'
            if r["seq_for_address"] > 1
            else ""
        )
        table_rows += f"""
        <tr>
            <td class="idx">{i}</td>
            <td>{escape(r['timestamp'] or '—')}</td>
            <td class="addr">{escape(r['from'])} {dup_badge}</td>
            <td class="amount">{r['amount']:.4f} {escape(r['symbol'])}</td>
            <td class="memo">{escape(r['memo'] or '')}</td>
            <td class="tx"><code>{escape(r['trx_id'][:12])}…</code></td>
        </tr>
        """
    return table_rows


def build_html(rows, period_start, period_end, used_endpoint, error_message, total_raw_count):
    """Собирает итоговую HTML-страницу из подготовленных строк."""

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if error_message:
        body_extra = f"""
        <div class="error-box">
            ⚠️ Не удалось получить данные ни с одной из публичных нод.<br>
            Последняя ошибка: {escape(error_message)}
        </div>
        """
    else:
        body_extra = ""

    captains_rows = rows[:TOP_GROUP_SIZE]
    members_rows = rows[TOP_GROUP_SIZE:TOP_GROUP_SIZE + SECOND_GROUP_SIZE]

    captains_table = render_table(captains_rows, "Поступлений в этой группе нет.")
    members_table = render_table(members_rows, "Поступлений в этой группе нет.")

    unique_addresses = len({r["from"] for r in rows})

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<link rel="icon" href="/favicon.png">
<title>Поступления USDCASH на {escape(ACCOUNT)}</title>
<style>
    :root {{
        --bg: #0f1115;
        --panel: #171a21;
        --border: #262b35;
        --text: #e6e8eb;
        --muted: #8a8f98;
        --accent: #4fd1c5;
        --accent2: #f6ad55;
    }}
    * {{ box-sizing: border-box; }}
    body {{
        margin: 0;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        background: var(--bg);
        color: var(--text);
        padding: 32px 16px;
    }}
    .wrap {{
        max-width: 980px;
        margin: 0 auto;
    }}
    h1 {{
        font-size: 22px;
        margin: 0 0 4px;
    }}
    .subtitle {{
        color: var(--muted);
        font-size: 14px;
        margin-bottom: 24px;
    }}
    .stats {{
        display: flex;
        gap: 16px;
        margin-bottom: 24px;
        flex-wrap: wrap;
    }}
    .stat-card {{
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 14px 18px;
        min-width: 150px;
    }}
    .stat-card .num {{
        font-size: 24px;
        font-weight: 700;
        color: var(--accent);
    }}
    .stat-card .label {{
        font-size: 12px;
        color: var(--muted);
        text-transform: uppercase;
        letter-spacing: 0.04em;
    }}
    table {{
        width: 100%;
        border-collapse: collapse;
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 10px;
        overflow: hidden;
        margin-bottom: 28px;
    }}
    th, td {{
        padding: 10px 14px;
        text-align: left;
        font-size: 13px;
        border-bottom: 1px solid var(--border);
    }}
    th {{
        color: var(--muted);
        text-transform: uppercase;
        font-size: 11px;
        letter-spacing: 0.04em;
        background: #1b1f28;
    }}
    tr:last-child td {{ border-bottom: none; }}
    .idx {{
        color: var(--muted);
        font-family: ui-monospace, monospace;
        width: 36px;
        text-align: right;
    }}
    .section-title {{
        font-size: 15px;
        margin: 0 0 10px;
        color: var(--text);
    }}
    .section-count {{
        color: var(--muted);
        font-size: 12px;
        font-weight: 400;
    }}
    .addr {{ font-family: ui-monospace, monospace; }}
    .amount {{ font-weight: 600; color: var(--accent); white-space: nowrap; }}
    .memo {{ color: var(--muted); max-width: 260px; overflow: hidden; text-overflow: ellipsis; }}
    .tx code {{ color: var(--muted); }}
    .badge {{
        display: inline-block;
        background: var(--accent2);
        color: #1a1a1a;
        font-size: 10px;
        font-weight: 700;
        padding: 1px 6px;
        border-radius: 8px;
        margin-left: 6px;
    }}
    .empty {{ text-align: center; color: var(--muted); padding: 30px 0; }}
    .error-box {{
        background: #3a1f1f;
        border: 1px solid #6b2c2c;
        color: #ffb3b3;
        padding: 14px 18px;
        border-radius: 10px;
        margin-bottom: 20px;
        font-size: 14px;
    }}
    .footer {{
        margin-top: 20px;
        color: var(--muted);
        font-size: 12px;
    }}
</style>
</head>
<body>
<div class="wrap">
    <h1>Поступления {escape(TOKEN_SYMBOL)} на {escape(ACCOUNT)}</h1>
    <div class="subtitle">
        Период: {escape(period_start)} — {escape(period_end)} ·
        Сформировано: {escape(generated_at)}
    </div>

    {body_extra}

    <div class="stats">
        <div class="stat-card">
            <div class="num">{len(rows)}</div>
            <div class="label">Показано записей</div>
        </div>
        <div class="stat-card">
            <div class="num">{unique_addresses}</div>
            <div class="label">Уникальных адресов</div>
        </div>
        <div class="stat-card">
            <div class="num">{total_raw_count}</div>
            <div class="label">Всего транзакций найдено</div>
        </div>
    </div>

    <h2 class="section-title">🥇 Капитаны <span class="section-count">(топ {TOP_GROUP_SIZE} по сумме)</span></h2>
    <table>
        <thead>
            <tr>
                <th class="idx">#</th>
                <th>Время (UTC)</th>
                <th>Отправитель</th>
                <th>Сумма</th>
                <th>Memo</th>
                <th>TX</th>
            </tr>
        </thead>
        <tbody>
            {captains_table}
        </tbody>
    </table>

    <h2 class="section-title">🥈 Участники <span class="section-count">(следующие {SECOND_GROUP_SIZE} по сумме)</span></h2>
    <table>
        <thead>
            <tr>
                <th class="idx">#</th>
                <th>Время (UTC)</th>
                <th>Отправитель</th>
                <th>Сумма</th>
                <th>Memo</th>
                <th>TX</th>
            </tr>
        </thead>
        <tbody>
            {members_table}
        </tbody>
    </table>

    <div class="footer">
        Источник данных: {escape(used_endpoint or "—")} ·
        Контракт токена: {escape(TOKEN_CONTRACT)} ·
        Диапазон суммы: ({MIN_AMOUNT}, {MAX_AMOUNT}), круглые суммы (кратные {ROUND_STEP}) отброшены ·
        Максимум {MAX_PER_ADDRESS} записей на один уникальный адрес · Сортировка: по сумме, по убыванию.
    </div>
</div>
</body>
</html>"""
    return html


# ============================== ОСНОВНАЯ ЛОГИКА ==============================


def main():
    now = datetime.now(timezone.utc)
    period_end_dt = now
    period_start_dt = now - timedelta(days=DAYS_BACK)

    after_iso = period_start_dt.strftime("%Y-%m-%dT%H:%M:%S")
    before_iso = period_end_dt.strftime("%Y-%m-%dT%H:%M:%S")

    print(f"[i] Запрашиваю переводы {TOKEN_CONTRACT}:transfer на {ACCOUNT}")
    print(f"[i] Период: {after_iso} .. {before_iso} (UTC)")

    raw_actions, used_endpoint, error_message = try_endpoints(
        ACCOUNT, TOKEN_CONTRACT, "transfer", after_iso, before_iso
    )

    if error_message:
        print(f"[!] Ошибка получения данных: {error_message}")

    print(f"[i] Получено сырых действий: {len(raw_actions)} (нода: {used_endpoint})")

    # Парсим и фильтруем
    transfers = []
    skipped_out_of_range = 0
    skipped_round = 0
    for action in raw_actions:
        parsed = parse_transfer(action)
        if not parsed:
            continue
        if parsed["to"] != ACCOUNT:
            continue
        if parsed["symbol"] != TOKEN_SYMBOL:
            continue

        amount = parsed["amount"]

        # Диапазон строго (MIN_AMOUNT, MAX_AMOUNT) — границы исключаем
        if not (MIN_AMOUNT < amount < MAX_AMOUNT):
            skipped_out_of_range += 1
            continue

        # Отбрасываем "круглые" суммы (кратные 0.5)
        if is_round_amount(amount):
            skipped_round += 1
            continue

        transfers.append(parsed)

    total_raw_count = len(transfers)
    print(f"[i] Переводов {TOKEN_SYMBOL} на {ACCOUNT} за период (после всех фильтров): {total_raw_count}")
    print(f"[i]   Отброшено вне диапазона ({MIN_AMOUNT}, {MAX_AMOUNT}): {skipped_out_of_range}")
    print(f"[i]   Отброшено как 'круглые' суммы: {skipped_round}")

    # Сортируем по времени по возрастанию — чтобы пометить #1/#2 по хронологии для каждого адреса
    transfers.sort(key=lambda t: t["timestamp"] or "")

    # Группируем по адресу, оставляем максимум MAX_PER_ADDRESS на адрес
    per_address_count = {}
    filtered_rows = []
    for t in transfers:
        addr = t["from"]
        count = per_address_count.get(addr, 0) + 1
        per_address_count[addr] = count
        if count <= MAX_PER_ADDRESS:
            t["seq_for_address"] = count
            filtered_rows.append(t)

    # Финальная сортировка вывода — по сумме, по убыванию
    filtered_rows.sort(key=lambda t: t["amount"], reverse=True)

    print(f"[i] Уникальных адресов: {len(per_address_count)}")
    print(f"[i] Записей в отчёте (после лимита {MAX_PER_ADDRESS}/адрес): {len(filtered_rows)}")

    html = build_html(
        filtered_rows,
        period_start_dt.strftime("%Y-%m-%d %H:%M UTC"),
        period_end_dt.strftime("%Y-%m-%d %H:%M UTC"),
        used_endpoint,
        error_message if not raw_actions else None,
        total_raw_count,
    )

    os.makedirs(os.path.dirname(OUTPUT_HTML) or ".", exist_ok=True)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"[✓] Отчёт сохранён: {OUTPUT_HTML}")


if __name__ == "__main__":
    main()
