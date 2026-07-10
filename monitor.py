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
  6. Из каждого уникального адреса оставляет МАКСИМУМ MAX_PER_ADDRESS переводов
     (самые ранние по времени), остальные дубли с того же адреса отбрасывает.
  7. Помечает "подозрительные" транзакции — те, что идут ВЫШЕ предыдущей
     по времени (против тренда снижения цены в редукционе).
  8. Сортирует итоговый список по сумме перевода — по убыванию.
  9. Генерирует HTML-страницу с двумя таблицами (Капитаны / Участники)
     и строкой-сводкой об остатке, который не вошёл в таблицы.
  10. Время отображается в часовом поясе UTC+DISPLAY_UTC_OFFSET (Казахстан UTC+5).

Запуск:
    python3 monitor.py

Зависимостей кроме стандартной библиотеки Python нет.
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
MAX_PER_ADDRESS = 10            # максимум переводов с одного адреса в выводе
DISPLAY_UTC_OFFSET = 5          # UTC+5 (Казахстан, Алматы/Астана)

MIN_AMOUNT = 10                 # нижняя граница суммы (строго больше)
MAX_AMOUNT = 60                 # верхняя граница суммы (строго меньше)
ROUND_STEP = 0.5                # суммы, кратные этому шагу, считаются "круглыми" и отбрасываются
ROUND_EPSILON = 1e-6            # допуск на погрешность float-сравнения при проверке кратности

TOP_GROUP_SIZE = 24             # сколько самых крупных переводов попадает в группу "Капитаны"
SECOND_GROUP_SIZE = 96          # сколько следующих по размеру переводов попадает в "Участники"

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
    Печатает прогресс по страницам, чтобы было видно, что скрипт не завис.
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

        if len(actions) < PAGE_LIMIT:
            break

        skip += PAGE_LIMIT
        page += 1

        if skip > 20000:
            break

    return all_actions


def format_timestamp(timestamp_raw):
    """
    Конвертирует ISO-timestamp из UTC в локальное время UTC+DISPLAY_UTC_OFFSET.
    Возвращает строку вида "2026-06-28 18:45 (UTC+5)".
    """
    if not timestamp_raw:
        return None
    try:
        dt = datetime.fromisoformat(timestamp_raw.replace("Z", "+00:00"))
        dt_local = dt + timedelta(hours=DISPLAY_UTC_OFFSET)
        return dt_local.strftime("%Y-%m-%d %H:%M") + f" (UTC+{DISPLAY_UTC_OFFSET})"
    except Exception:
        return timestamp_raw


def parse_transfer(action):
    """
    Достаёт из сырого action нужные поля: from, to, quantity, symbol, memo, время, tx id.
    Возвращает None, если структура не похожа на обычный transfer.
    Время конвертируется в локальный часовой пояс (UTC+DISPLAY_UTC_OFFSET).
    """
    act = action.get("act", {})
    data = act.get("data", {})

    frm = data.get("from")
    to = data.get("to")
    quantity_raw = data.get("quantity")
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

    timestamp_raw = action.get("@timestamp") or action.get("timestamp")
    timestamp = format_timestamp(timestamp_raw)
    # Сохраняем сырой timestamp для сортировки (всегда UTC ISO)
    timestamp_sort = timestamp_raw or ""

    trx_id = action.get("trx_id", "")

    return {
        "from": frm,
        "to": to,
        "amount": amount,
        "symbol": symbol,
        "memo": memo,
        "timestamp": timestamp,
        "timestamp_sort": timestamp_sort,
        "trx_id": trx_id,
        "suspicious": False,
    }


def is_round_amount(amount):
    """
    Возвращает True, если сумма "круглая" — кратна ROUND_STEP (0.5).
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


def mark_suspicious(transfers_by_time):
    """
    Помечает транзакции, идущие ПРОТИВ тренда снижения цены в редукционе.
    Редукцион — цена падает со временем, поэтому каждый следующий взнос
    по времени должен быть <= предыдущего. Если пришло БОЛЬШЕ — подозрительно,
    скорее всего это системный/нецелевой перевод, а не взнос игрока.
    transfers_by_time — список уже отсортированный по timestamp (от старых к новым).
    """
    prev_amount = None
    for t in transfers_by_time:
        if prev_amount is not None and t["amount"] > prev_amount:
            t["suspicious"] = True
        else:
            t["suspicious"] = False
        prev_amount = t["amount"]


def render_table(rows, empty_message):
    """
    Рендерит одну HTML-таблицу со сквозной нумерацией строк слева (#1, #2, ...).
    Подозрительные строки (suspicious=True) выделяются цветом.
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
        row_class = ' class="suspicious"' if r.get("suspicious") else ""
        susp_icon = " ⚠️" if r.get("suspicious") else ""

        table_rows += f"""
        <tr{row_class}>
            <td class="idx">{i}{susp_icon}</td>
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

    now_local = datetime.now(timezone.utc) + timedelta(hours=DISPLAY_UTC_OFFSET)
    generated_at = now_local.strftime("%Y-%m-%d %H:%M:%S") + f" (UTC+{DISPLAY_UTC_OFFSET})"

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
    remainder_count = max(0, len(rows) - TOP_GROUP_SIZE - SECOND_GROUP_SIZE)

    captains_table = render_table(captains_rows, "Поступлений в этой группе нет.")
    members_table = render_table(members_rows, "Поступлений в этой группе нет.")

    if remainder_count > 0:
        remainder_html = f'<div class="remainder">И ещё <strong>{remainder_count}</strong> записей не вошли в таблицы</div>'
    else:
        remainder_html = ""

    unique_addresses = len({r["from"] for r in rows})
    suspicious_count = sum(1 for r in rows if r.get("suspicious"))

    susp_legend = ""
    if suspicious_count > 0:
        susp_legend = '<div class="susp-legend">⚠️ Подозрительные строки (выделены оранжевым) — сумма выше предыдущей по времени, что противоречит тренду снижения цены в редукционе. Возможно, это системные переводы, а не взносы игроков.</div>'

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
        --danger: #f66464;
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
    .stat-card .num.warn {{ color: var(--accent2); }}
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
        margin-bottom: 12px;
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
        width: 48px;
        text-align: right;
        white-space: nowrap;
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
    tr.suspicious td {{ background: rgba(246, 100, 100, 0.07); }}
    tr.suspicious .amount {{ color: var(--accent2); }}
    tr.suspicious .idx {{ color: var(--accent2); }}
    .susp-legend {{
        font-size: 12px;
        color: var(--muted);
        margin-bottom: 20px;
        padding: 8px 12px;
        background: rgba(246, 100, 100, 0.07);
        border-left: 3px solid var(--accent2);
        border-radius: 4px;
    }}
    .remainder {{
        text-align: center;
        color: var(--muted);
        font-size: 13px;
        padding: 14px;
        border: 1px dashed var(--border);
        border-radius: 10px;
        margin-bottom: 28px;
    }}
    .remainder strong {{ color: var(--text); }}
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
        <div class="stat-card">
            <div class="num warn">{suspicious_count}</div>
            <div class="label">⚠️ Подозрительных</div>
        </div>
    </div>

    {susp_legend}

    <h2 class="section-title">🥇 Капитаны <span class="section-count">(топ {TOP_GROUP_SIZE} по сумме)</span></h2>
    <table>
        <thead>
            <tr>
                <th class="idx">#</th>
                <th>Время (UTC+{DISPLAY_UTC_OFFSET})</th>
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
                <th>Время (UTC+{DISPLAY_UTC_OFFSET})</th>
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

    {remainder_html}

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

        if not (MIN_AMOUNT < amount < MAX_AMOUNT):
            skipped_out_of_range += 1
            continue

        if is_round_amount(amount):
            skipped_round += 1
            continue

        transfers.append(parsed)

    total_raw_count = len(transfers)
    print(f"[i] Переводов {TOKEN_SYMBOL} на {ACCOUNT} за период (после всех фильтров): {total_raw_count}")
    print(f"[i]   Отброшено вне диапазона ({MIN_AMOUNT}, {MAX_AMOUNT}): {skipped_out_of_range}")
    print(f"[i]   Отброшено как 'круглые' суммы: {skipped_round}")

    # Сортируем по времени по возрастанию — для хронологии адресов и mark_suspicious
    transfers.sort(key=lambda t: t["timestamp_sort"] or "")

    # Помечаем подозрительные транзакции (против тренда снижения редукциона)
    mark_suspicious(transfers)

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

    # Финальная сортировка — по сумме, по убыванию
    filtered_rows.sort(key=lambda t: t["amount"], reverse=True)

    suspicious_in_output = sum(1 for r in filtered_rows if r.get("suspicious"))
    print(f"[i] Уникальных адресов: {len(per_address_count)}")
    print(f"[i] Записей в отчёте (после лимита {MAX_PER_ADDRESS}/адрес): {len(filtered_rows)}")
    print(f"[i] Из них подозрительных (против тренда): {suspicious_in_output}")

    # Период тоже показываем в локальном времени
    period_start_local = (period_start_dt + timedelta(hours=DISPLAY_UTC_OFFSET)).strftime("%Y-%m-%d %H:%M") + f" (UTC+{DISPLAY_UTC_OFFSET})"
    period_end_local = (period_end_dt + timedelta(hours=DISPLAY_UTC_OFFSET)).strftime("%Y-%m-%d %H:%M") + f" (UTC+{DISPLAY_UTC_OFFSET})"

    html = build_html(
        filtered_rows,
        period_start_local,
        period_end_local,
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
