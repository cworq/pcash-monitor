#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Мониторинг поступлений USDCASH на аккаунт 4store.pcash.
Генерирует public/data.json — его читает index.html через fetch каждые 15 минут.
Vercel деплоится один раз (index.html), данные обновляются без перезапуска Vercel.
"""

import json
import os
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone

# ============================== НАСТРОЙКИ ==============================

ACCOUNT = "4store.pcash"
TOKEN_CONTRACT = "token.pcash"
TOKEN_SYMBOL = "USDCASH"
DAYS_BACK = 7
MAX_PER_ADDRESS = 10
DISPLAY_UTC_OFFSET = 5

MIN_AMOUNT = 10
MAX_AMOUNT = 60
ROUND_STEP = 0.5
ROUND_EPSILON = 1e-6

TOP_GROUP_SIZE = 24
SECOND_GROUP_SIZE = 96

# Параметры редукциона
# ОБНОВЛЯЙ AUCTION_START_UTC в начале каждого нового турнира!
# Формула: (100 - текущая_цена) / 0.005 = минут прошло, вычти из текущего времени UTC
AUCTION_START_UTC = "2026-07-02T05:10:36"
AUCTION_START_PRICE = 100.0
AUCTION_PRICE_PER_MIN = 0.005
AUCTION_MIN_PRICE = 0.68
SUSPICIOUS_THRESHOLD = 0.5

HYPERION_ENDPOINTS = [
    "https://hyperion.paycash.online",
    "https://eos.hyperion.eosrio.io",
    "https://eos.eosusa.io",
]

OUTPUT_JSON = "public/data.json"
PAGE_LIMIT = 100
HTTP_TIMEOUT = 15

# ============================== ВСПОМОГАТЕЛЬНОЕ ==============================


def parse_dt(s):
    """Универсальный парсер дат из Hyperion. Всегда возвращает aware datetime UTC."""
    if not s:
        return None
    s2 = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s2)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s2[:19])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_timestamp(timestamp_raw):
    """UTC ISO → локальное время UTC+DISPLAY_UTC_OFFSET для отображения."""
    dt = parse_dt(timestamp_raw)
    if dt is None:
        return timestamp_raw or ""
    dt_local = dt + timedelta(hours=DISPLAY_UTC_OFFSET)
    return dt_local.strftime("%Y-%m-%d %H:%M") + f" (UTC+{DISPLAY_UTC_OFFSET})"


def expected_price_at(timestamp_raw):
    """Ожидаемая цена редукциона в момент транзакции."""
    tx_time = parse_dt(timestamp_raw)
    if tx_time is None:
        return None
    auction_start = parse_dt(AUCTION_START_UTC)
    minutes = (tx_time - auction_start).total_seconds() / 60
    if minutes < 0:
        return None
    price = AUCTION_START_PRICE - minutes * AUCTION_PRICE_PER_MIN
    return round(max(price, AUCTION_MIN_PRICE), 4)


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


def main():
    now = datetime.now(timezone.utc)
    period_end_dt = now
    period_start_dt = now - timedelta(days=DAYS_BACK)

    after_iso = period_start_dt.strftime("%Y-%m-%dT%H:%M:%S")
    before_iso = period_end_dt.strftime("%Y-%m-%dT%H:%M:%S")

    print(f"[i] Запрашиваю переводы {TOKEN_CONTRACT}:transfer на {ACCOUNT}")
    print(f"[i] Период: {after_iso} .. {before_iso} (UTC)")

    now_exp = expected_price_at(now.strftime("%Y-%m-%dT%H:%M:%S"))
    print(f"[i] Ожидаемая цена редукциона сейчас: ${now_exp:.3f}" if now_exp else "[!] expected_price вернул None")

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

    # Сортировка по времени для seq_for_address
    transfers.sort(key=lambda t: t["timestamp_sort"] or "")

    # Добавляем expected_price, price_diff, suspicious, seq_for_address
    per_address_count = {}
    filtered_rows = []
    for t in transfers:
        addr = t["from"]
        count = per_address_count.get(addr, 0) + 1
        per_address_count[addr] = count
        if count <= MAX_PER_ADDRESS:
            expected = expected_price_at(t["timestamp_sort"])
            diff = round(t["amount"] - expected, 4) if expected is not None else None
            t["seq_for_address"] = count
            t["expected_price"] = expected
            t["price_diff"] = diff
            t["suspicious"] = (diff is not None and diff > SUSPICIOUS_THRESHOLD)
            filtered_rows.append(t)

    # Сортировка по сумме по убыванию
    filtered_rows.sort(key=lambda t: t["amount"], reverse=True)

    suspicious_count = sum(1 for r in filtered_rows if r.get("suspicious"))
    remainder_count = max(0, len(filtered_rows) - TOP_GROUP_SIZE - SECOND_GROUP_SIZE)

    print(f"[i] Уникальных адресов: {len(per_address_count)}")
    print(f"[i] Записей в отчёте: {len(filtered_rows)}, подозрительных: {suspicious_count}")

    now_local = now + timedelta(hours=DISPLAY_UTC_OFFSET)
    period_start_local = (period_start_dt + timedelta(hours=DISPLAY_UTC_OFFSET)).strftime("%Y-%m-%d %H:%M") + f" (UTC+{DISPLAY_UTC_OFFSET})"
    period_end_local = (now + timedelta(hours=DISPLAY_UTC_OFFSET)).strftime("%Y-%m-%d %H:%M") + f" (UTC+{DISPLAY_UTC_OFFSET})"

    output = {
        "generated_at": now_local.strftime("%Y-%m-%d %H:%M:%S") + f" (UTC+{DISPLAY_UTC_OFFSET})",
        "period_start": period_start_local,
        "period_end": period_end_local,
        "endpoint": used_endpoint or "",
        "error": error_message if not raw_actions else None,
        "total_raw_count": total_raw_count,
        "unique_addresses": len(per_address_count),
        "suspicious_count": suspicious_count,
        "remainder_count": remainder_count,
        "current_auction_price": now_exp,
        "top_group_size": TOP_GROUP_SIZE,
        "second_group_size": SECOND_GROUP_SIZE,
        "suspicious_threshold": SUSPICIOUS_THRESHOLD,
        "rows": filtered_rows,
    }

    os.makedirs(os.path.dirname(OUTPUT_JSON) or ".", exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"[✓] Данные сохранены: {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
