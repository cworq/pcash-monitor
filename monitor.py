#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Мониторинг поступлений USDCASH на аккаунт 4store.pcash (сеть Vaulta / ex-EOS).

Подозрительные транзакции определяются математически: для каждого взноса
вычисляется ожидаемая цена редукциона в момент транзакции (на основе даты
старта и скорости снижения). Если реальная сумма превышает ожидаемую более
чем на SUSPICIOUS_THRESHOLD — взнос помечается как подозрительный.
"""

import json
import os
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone
from html import escape

# ============================== НАСТРОЙКИ ==============================

ACCOUNT = "4store.pcash"
TOKEN_CONTRACT = "token.pcash"
TOKEN_SYMBOL = "USDCASH"
DAYS_BACK = 7
MAX_PER_ADDRESS = 10
DISPLAY_UTC_OFFSET = 5          # UTC+5 (Казахстан)

MIN_AMOUNT = 10
MAX_AMOUNT = 60
ROUND_STEP = 0.5
ROUND_EPSILON = 1e-6

TOP_GROUP_SIZE = 24
SECOND_GROUP_SIZE = 96

# --- Параметры редукциона ---
# Дата и время старта текущего редукциона (UTC).
# ОБНОВЛЯЙ ЭТУ ДАТУ в начале каждого нового турнира!
# Способ вычислить: (стартовая_цена - текущая_цена) / 0.005 = минут с начала.
AUCTION_START_UTC = "2026-07-02T05:10:36"
AUCTION_START_PRICE = 100.0     # цена в момент старта ($)
AUCTION_PRICE_PER_MIN = 0.005   # снижение в минуту ($)
AUCTION_MIN_PRICE = 0.68        # минимальная цена редукциона ($)
SUSPICIOUS_THRESHOLD = 2.0      # если взнос выше ожидаемой цены на эту сумму — подозрительно

HYPERION_ENDPOINTS = [
    "https://hyperion.paycash.online",
    "https://eos.hyperion.eosrio.io",
    "https://eos.eosusa.io",
]

OUTPUT_HTML = "public/index.html"
PAGE_LIMIT = 100
HTTP_TIMEOUT = 15

# ============================== ВСПОМОГАТЕЛЬНОЕ ==============================


def http_get_json(url, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": "pcash-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def fetch_actions(endpoint, account, contract, action_name, after_iso, before_iso):
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
        print(f"        получено {len(actions)} (всего: {len(all_actions)})", flush=True)
        if len(actions) < PAGE_LIMIT:
            break
        skip += PAGE_LIMIT
        page += 1
        if skip > 20000:
            break
    return all_actions


def format_timestamp(timestamp_raw):
    """UTC ISO → локальное время UTC+DISPLAY_UTC_OFFSET."""
    if not timestamp_raw:
        return None
    try:
        dt = datetime.fromisoformat(timestamp_raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_local = dt + timedelta(hours=DISPLAY_UTC_OFFSET)
        return dt_local.strftime("%Y-%m-%d %H:%M") + f" (UTC+{DISPLAY_UTC_OFFSET})"
    except Exception:
        return timestamp_raw


def parse_transfer(action):
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
    return {
        "from": frm,
        "to": to,
        "amount": amount,
        "symbol": symbol,
        "memo": memo,
        "timestamp": format_timestamp(timestamp_raw),
        "timestamp_sort": timestamp_raw or "",
        "trx_id": action.get("trx_id", ""),
        "suspicious": False,
        "expected_price": None,
        "price_diff": None,
    }


def is_round_amount(amount):
    remainder = round(amount / ROUND_STEP) * ROUND_STEP
    return abs(amount - remainder) < ROUND_EPSILON


def try_endpoints(account, contract, action_name, after_iso, before_iso):
    last_error = None
    for endpoint in HYPERION_ENDPOINTS:
        try:
            actions = fetch_actions(endpoint, account, contract, action_name, after_iso, before_iso)
            return actions, endpoint, None
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
            last_error = f"{endpoint}: {e}"
    return [], None, last_error


def expected_price_at(timestamp_raw):
    """
    Вычисляет ожидаемую цену редукциона в момент транзакции.
    Цена = AUCTION_START_PRICE - минуты_с_начала * AUCTION_PRICE_PER_MIN,
    но не ниже AUCTION_MIN_PRICE.
    Возвращает None если транзакция до старта редукциона.
    """
    if not timestamp_raw:
        return None
    try:
        auction_start = datetime.fromisoformat(AUCTION_START_UTC).replace(tzinfo=timezone.utc)
        tx_time = datetime.fromisoformat(timestamp_raw.replace("Z", "+00:00"))
        if tx_time.tzinfo is None:
            tx_time = tx_time.replace(tzinfo=timezone.utc)
        minutes = (tx_time - auction_start).total_seconds() / 60
        if minutes < 0:
            return None  # до старта редукциона
        price = AUCTION_START_PRICE - minutes * AUCTION_PRICE_PER_MIN
        return round(max(price, AUCTION_MIN_PRICE), 4)
    except Exception:
        return None


def mark_suspicious(transfers):
    """
    Для каждой транзакции вычисляет ожидаемую цену редукциона и отклонение.
    Если реальная сумма выше ожидаемой на SUSPICIOUS_THRESHOLD — подозрительно.
    Если дата старта редукциона не покрывает транзакцию — используем резервный
    метод: взнос выше предыдущего по времени.
    """
    prev_amount = None
    for t in transfers:
        expected = expected_price_at(t["timestamp_sort"])
        t["expected_price"] = expected

        if expected is not None:
            diff = round(t["amount"] - expected, 4)
            t["price_diff"] = diff
            t["suspicious"] = diff > SUSPICIOUS_THRESHOLD
        else:
            # Резервный метод
            t["price_diff"] = None
            t["suspicious"] = (prev_amount is not None and t["amount"] > prev_amount + SUSPICIOUS_THRESHOLD)

        prev_amount = t["amount"]


def render_table(rows, empty_message):
    if not rows:
        return f'<tr><td colspan="7" class="empty">{escape(empty_message)}</td></tr>'

    table_rows = ""
    for i, r in enumerate(rows, start=1):
        dup_badge = (
            f'<span class="badge">#{r["seq_for_address"]}</span>'
            if r["seq_for_address"] > 1
            else ""
        )
        row_class = ' class="suspicious"' if r.get("suspicious") else ""
        susp_icon = " ⚠️" if r.get("suspicious") else ""

        # Колонка: ожидаемая цена и отклонение
        expected = r.get("expected_price")
        diff = r.get("price_diff")
        if expected is not None and diff is not None:
            sign = "+" if diff > 0 else ""
            if diff > SUSPICIOUS_THRESHOLD:
                diff_color = "var(--danger)"
            elif diff > 0:
                diff_color = "var(--accent2)"
            else:
                diff_color = "var(--accent)"
            price_cell = (
                f'<span style="color:var(--muted)">{expected:.3f}</span>'
                f' <span style="color:{diff_color};font-size:11px;font-weight:600">({sign}{diff:.3f})</span>'
            )
        else:
            price_cell = '<span style="color:var(--muted)">—</span>'

        table_rows += f"""
        <tr{row_class}>
            <td class="idx">{i}{susp_icon}</td>
            <td class="ts">{escape(r['timestamp'] or '—')}</td>
            <td class="addr">{escape(r['from'])} {dup_badge}</td>
            <td class="amount">{r['amount']:.4f} {escape(r['symbol'])}</td>
            <td class="price">{price_cell}</td>
            <td class="memo">{escape(r['memo'] or '')}</td>
            <td class="tx"><code>{escape(r['trx_id'][:12])}…</code></td>
        </tr>
        """
    return table_rows


def build_html(rows, period_start, period_end, used_endpoint, error_message, total_raw_count):
    now_local = datetime.now(timezone.utc) + timedelta(hours=DISPLAY_UTC_OFFSET)
    generated_at = now_local.strftime("%Y-%m-%d %H:%M:%S") + f" (UTC+{DISPLAY_UTC_OFFSET})"

    body_extra = ""
    if error_message:
        body_extra = f"""<div class="error-box">
            ⚠️ Не удалось получить данные ни с одной из публичных нод.<br>
            Последняя ошибка: {escape(error_message)}
        </div>"""

    captains_rows = rows[:TOP_GROUP_SIZE]
    members_rows = rows[TOP_GROUP_SIZE:TOP_GROUP_SIZE + SECOND_GROUP_SIZE]
    remainder_count = max(0, len(rows) - TOP_GROUP_SIZE - SECOND_GROUP_SIZE)

    captains_table = render_table(captains_rows, "Поступлений в этой группе нет.")
    members_table = render_table(members_rows, "Поступлений в этой группе нет.")

    remainder_html = ""
    if remainder_count > 0:
        remainder_html = f'<div class="remainder">И ещё <strong>{remainder_count}</strong> записей не вошли в таблицы</div>'

    unique_addresses = len({r["from"] for r in rows})
    suspicious_count = sum(1 for r in rows if r.get("suspicious"))

    susp_legend = ""
    if suspicious_count > 0:
        susp_legend = f"""<div class="susp-legend">
            ⚠️ <strong>{suspicious_count} подозрительных</strong> — сумма взноса превышает
            расчётную цену редукциона в момент транзакции более чем на ${SUSPICIOUS_THRESHOLD:.1f}.
            Колонка <em>«Ожид. (откл.)»</em>: серая цифра — ожидаемая цена, цветная — отклонение
            (<span style="color:#4fd1c5">зелёный</span> = ниже нормы,
            <span style="color:#f6ad55">оранжевый</span> = чуть выше,
            <span style="color:#f66464">красный</span> = подозрительно выше).
        </div>"""

    # Текущая ожидаемая цена редукциона для справки в футере
    now_expected = expected_price_at(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"))
    now_expected_str = f"${now_expected:.3f}" if now_expected is not None else "н/д"

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
    .wrap {{ max-width: 1100px; margin: 0 auto; }}
    h1 {{ font-size: 22px; margin: 0 0 4px; }}
    .subtitle {{ color: var(--muted); font-size: 14px; margin-bottom: 24px; }}
    .stats {{ display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap; }}
    .stat-card {{
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 14px 18px;
        min-width: 150px;
    }}
    .stat-card .num {{ font-size: 24px; font-weight: 700; color: var(--accent); }}
    .stat-card .num.warn {{ color: var(--accent2); }}
    .stat-card .num.price {{ color: #b794f4; }}
    .stat-card .label {{ font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }}
    table {{
        width: 100%;
        border-collapse: collapse;
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 10px;
        overflow: hidden;
        margin-bottom: 12px;
    }}
    th, td {{ padding: 10px 14px; text-align: left; font-size: 13px; border-bottom: 1px solid var(--border); }}
    th {{ color: var(--muted); text-transform: uppercase; font-size: 11px; letter-spacing: 0.04em; background: #1b1f28; }}
    tr:last-child td {{ border-bottom: none; }}
    .idx {{ color: var(--muted); font-family: ui-monospace, monospace; width: 48px; text-align: right; white-space: nowrap; }}
    .ts {{ white-space: nowrap; font-size: 12px; }}
    .section-title {{ font-size: 15px; margin: 0 0 10px; color: var(--text); }}
    .section-count {{ color: var(--muted); font-size: 12px; font-weight: 400; }}
    .addr {{ font-family: ui-monospace, monospace; }}
    .amount {{ font-weight: 600; color: var(--accent); white-space: nowrap; }}
    .price {{ font-family: ui-monospace, monospace; font-size: 12px; white-space: nowrap; }}
    .memo {{ color: var(--muted); max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .tx code {{ color: var(--muted); font-size: 11px; }}
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
    tr.suspicious .idx {{ color: var(--danger); }}
    .susp-legend {{
        font-size: 12px;
        color: var(--muted);
        margin-bottom: 20px;
        padding: 10px 14px;
        background: rgba(246, 100, 100, 0.07);
        border-left: 3px solid var(--accent2);
        border-radius: 4px;
        line-height: 1.7;
    }}
    .susp-legend strong {{ color: var(--accent2); }}
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
    .footer {{ margin-top: 20px; color: var(--muted); font-size: 12px; line-height: 1.8; }}
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
        <div class="stat-card">
            <div class="num price">{now_expected_str}</div>
            <div class="label">Цена редукциона сейчас</div>
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
                <th>Ожид. (откл.)</th>
                <th>Memo</th>
                <th>TX</th>
            </tr>
        </thead>
        <tbody>{captains_table}</tbody>
    </table>

    <h2 class="section-title">🥈 Участники <span class="section-count">(следующие {SECOND_GROUP_SIZE} по сумме)</span></h2>
    <table>
        <thead>
            <tr>
                <th class="idx">#</th>
                <th>Время (UTC+{DISPLAY_UTC_OFFSET})</th>
                <th>Отправитель</th>
                <th>Сумма</th>
                <th>Ожид. (откл.)</th>
                <th>Memo</th>
                <th>TX</th>
            </tr>
        </thead>
        <tbody>{members_table}</tbody>
    </table>

    {remainder_html}

    <div class="footer">
        Источник данных: {escape(used_endpoint or "—")} ·
        Контракт токена: {escape(TOKEN_CONTRACT)} ·
        Диапазон суммы: ({MIN_AMOUNT}, {MAX_AMOUNT}) · Круглые суммы (кратные {ROUND_STEP}) отброшены ·
        Макс. {MAX_PER_ADDRESS} записей на адрес · Сортировка: по сумме по убыванию<br>
        Редукцион: старт {escape(AUCTION_START_UTC)} UTC · начальная цена ${AUCTION_START_PRICE} ·
        снижение ${AUCTION_PRICE_PER_MIN}/мин · мин. цена ${AUCTION_MIN_PRICE} ·
        порог подозрительности: +${SUSPICIOUS_THRESHOLD} от ожидаемой цены<br>
        ⚠️ При начале нового турнира обнови <code>AUCTION_START_UTC</code> в настройках скрипта.
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
    now_exp = expected_price_at(now.strftime("%Y-%m-%dT%H:%M:%S"))
    print(f"[i] Ожидаемая цена редукциона сейчас: ${now_exp:.3f}" if now_exp else "[i] Редукцион ещё не стартовал")

    raw_actions, used_endpoint, error_message = try_endpoints(
        ACCOUNT, TOKEN_CONTRACT, "transfer", after_iso, before_iso
    )

    if error_message:
        print(f"[!] Ошибка: {error_message}")

    print(f"[i] Получено сырых действий: {len(raw_actions)} (нода: {used_endpoint})")

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
    print(f"[i] После фильтров: {total_raw_count} (вне диапазона: {skipped_out_of_range}, круглые: {skipped_round})")

    transfers.sort(key=lambda t: t["timestamp_sort"] or "")
    mark_suspicious(transfers)

    per_address_count = {}
    filtered_rows = []
    for t in transfers:
        addr = t["from"]
        count = per_address_count.get(addr, 0) + 1
        per_address_count[addr] = count
        if count <= MAX_PER_ADDRESS:
            t["seq_for_address"] = count
            filtered_rows.append(t)

    filtered_rows.sort(key=lambda t: t["amount"], reverse=True)

    suspicious_count = sum(1 for r in filtered_rows if r.get("suspicious"))
    print(f"[i] Уникальных адресов: {len(per_address_count)}")
    print(f"[i] Записей в отчёте: {len(filtered_rows)}")
    print(f"[i] Подозрительных: {suspicious_count}")

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
