"""Повторная проверка остатков и отзывов с ручной остановкой."""

from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

from kaspi_card import Client, QYZYLORDA, _headers, _reported_stock
from kaspi_search import search_cards
from kaspi_store import Store, now_iso

# Столько запросов к Kaspi идут одновременно. При ~0,9 с на запрос и 5 запросах
# на карточку 1000 карточек занимают около 300 000 мс.
READ_PARALLEL = 16

DELIVERY_NAMES = {
    "TO_DOOR": "доставка",
    "PICKUP": "самовывоз",
    "POSTOMAT": "постомат",
    "EXPRESS": "экспресс",
}
MONTHS = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def _day_label(days: int | None, moment: datetime | None) -> str:
    if days == 0:
        text = "сегодня"
    elif days == 1:
        text = "завтра"
    elif isinstance(days, int):
        text = f"через {days} дн."
    else:
        text = "срок не указан"
    if moment is not None:
        text = f"{text}, {moment.day} {MONTHS[moment.month - 1]}"
    return text


def _parse_moment(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=QYZYLORDA)
    return moment.astimezone(QYZYLORDA)


def delivery_rows(offer: dict[str, Any]) -> list[dict[str, Any]]:
    options = offer.get("deliveryOptions")
    if not isinstance(options, dict):
        duration = offer.get("deliveryDuration")
        if duration == "TODAY":
            return [{"type": offer.get("deliveryType") or "TO_DOOR", "label": "сегодня", "cost": None}]
        if duration == "TOMORROW":
            return [{"type": offer.get("deliveryType") or "TO_DOOR", "label": "завтра", "cost": None}]
        return []
    preferred = offer.get("deliveryType")
    keys = [preferred] if isinstance(preferred, str) else []
    keys.extend(key for key in ("TO_DOOR", "EXPRESS", "POSTOMAT", "PICKUP") if key not in keys)
    rows = []
    for key in keys:
        option = options.get(key)
        if not isinstance(option, dict):
            continue
        days = option.get("deliveryDays") if isinstance(option.get("deliveryDays"), int) else None
        moment = _parse_moment(option.get("delivery"))
        name = DELIVERY_NAMES.get(key, key)
        rows.append(
            {
                "type": key,
                "label": f"{_day_label(days, moment)}, {name}",
                "cost": option.get("deliveryCost") if isinstance(option.get("deliveryCost"), (int, float)) else None,
            }
        )
    return rows


def _offers(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    inner = payload.get("offers")
    if isinstance(inner, dict) and isinstance(inner.get("offers"), list):
        return [item for item in inner["offers"] if isinstance(item, dict)]
    if isinstance(inner, list):
        return [item for item in inner if isinstance(item, dict)]
    return []


def _stars(summary: dict[str, Any]) -> dict[str, int]:
    shares = summary.get("ratingShares") or {}
    stars: dict[str, int] = {}
    for mark in ("1", "2", "3", "4", "5"):
        slot = shares.get(mark) or {}
        count = slot.get("count") if isinstance(slot, dict) else 0
        stars[mark] = int(count) if isinstance(count, int) else 0
    return stars


def thousand_card_ms(elapsed_ms: int, cards: int) -> int | None:
    """Сколько миллисекунд заняла бы такая же обработка 1000 карточек."""
    if cards <= 0 or elapsed_ms < 0:
        return None
    return int(round(elapsed_ms * 1000 / cards))


class RequestLane:
    """Очередь отдельных сессий. Один и тот же клиент из двух потоков не вызывается."""

    def __init__(self, client: Client, size: int = READ_PARALLEL) -> None:
        self._slots: queue.Queue[Client] = queue.Queue()
        spawn = getattr(client, "spawn", None)
        if callable(spawn):
            for _ in range(size):
                self._slots.put(spawn())
            self._shared: Client | None = None
            self._guard = None
        else:
            self._shared = client
            self._guard = threading.Lock()

    def request(self, *args: Any, **kwargs: Any) -> tuple[int, Any | None, str | None, int]:
        if self._shared is not None and self._guard is not None:
            with self._guard:
                return self._shared.request(*args, **kwargs)
        worker = self._slots.get()
        try:
            return worker.request(*args, **kwargs)
        finally:
            self._slots.put(worker)


def snapshot_card(client: Client | RequestLane, product_id: str, city: str) -> dict[str, Any]:
    """Один проход по карточке: продавцы, склад, сводка отзывов. Оплату не вызывает."""
    page_url = f"https://kaspi.kz/shop/p/-{product_id}/?c={city}"
    headers = _headers(page_url, city)
    status, payload, error, _attempts = client.request(
        "POST",
        f"https://kaspi.kz/yml/bff-sf/item-page/product/{product_id}",
        headers={**headers, "Content-Type": "application/json; charset=UTF-8"},
        params={"sku": product_id},
        json_body={"cityId": city, "zoneId": [], "limit": 30, "page": 0, "sortOption": "PRICE"},
    )
    if status != 200 or not isinstance(payload, dict):
        return {"ok": False, "error": error or f"карточка ответила HTTP {status}"}

    offers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for offer in _offers(payload)[:5]:
        merchant_id = str(offer.get("merchantId") or "")
        if not merchant_id or merchant_id in seen:
            continue
        seen.add(merchant_id)
        offers.append(offer)

    def read_stock(offer: dict[str, Any]) -> dict[str, Any]:
        merchant_id = str(offer.get("merchantId") or "")
        stock_status, stock_body, stock_error, _stock_attempts = client.request(
            "GET",
            "https://kaspi.kz/yml/cart/shop/max-quantity",
            headers=headers,
            params={
                "sku": product_id,
                "uid": merchant_id,
                "unit": "PIECES",
                "increment": 1,
                "quantity": 50,
            },
        )
        stock = _reported_stock(stock_body) if stock_status == 200 else None
        return {
            "merchant_id": merchant_id,
            "merchant_name": offer.get("merchantName") or merchant_id,
            "price": offer.get("price") if isinstance(offer.get("price"), (int, float)) else None,
            "stock": stock,
            "stock_error": stock_error if stock is None else None,
            "delivery": delivery_rows(offer),
        }

    def read_reviews() -> tuple[int, Any, str | None]:
        return client.request(
            "POST",
            "https://review.kaspi.kz/shop/display/api/api/v2/review-summary/product",
            headers={**headers, "Content-Type": "application/json", "Content-Language": "ru-RU"},
            json_body={"productId": product_id, "categoryCode": None},
        )

    with ThreadPoolExecutor(max_workers=max(1, len(offers) + 1)) as pool:
        stock_jobs = [pool.submit(read_stock, offer) for offer in offers]
        review_job = pool.submit(read_reviews)
        merchants = [job.result() for job in stock_jobs]
        review_status, summary, review_error, _review_attempts = review_job.result()
    review_count = None
    stars = {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
    if review_status == 200 and isinstance(summary, dict):
        if isinstance(summary.get("ratingCount"), int):
            review_count = summary["ratingCount"]
        stars = _stars(summary)
    known_stock = [item["stock"] for item in merchants if isinstance(item.get("stock"), int)]
    return {
        "ok": True,
        "error": review_error,
        "stock_total": sum(known_stock) if known_stock else None,
        "review_count": review_count,
        "stars": stars,
        "merchants": merchants,
    }


class Watcher:
    def __init__(self, store: Store, client: Client) -> None:
        self.store = store
        self.client = client
        self._stops: dict[int, threading.Event] = {}
        self._primed: set[int] = set()
        self._guard = threading.Lock()

    def list_now(self, query_id: int) -> None:
        """Ищет карточки в этом же запросе, чтобы список сохранился до фоновой проверки."""
        query = self.store.get_query(query_id)
        self.store.update_query(query_id, status="running", progress="ищу карточки сразу по всем городам", last_error=None)
        found = search_cards(self.client, query["name"], query["city_id"])
        if found["error"]:
            raise RuntimeError(found["error"])
        self.store.update_query(query_id, search_total=found["total"])
        for card in found["cards"]:
            self.store.upsert_card(query_id, card)
        with self._guard:
            self._primed.add(query_id)
        if not found["cards"]:
            self.store.update_query(query_id, progress="по этому наименованию карточек нет")

    def start(self, query_id: int) -> None:
        stop = threading.Event()
        with self._guard:
            previous = self._stops.get(query_id)
            if previous is not None:
                previous.set()
            self._stops[query_id] = stop
        threading.Thread(target=self._loop, args=(query_id, stop), name=f"kaspi-query-{query_id}", daemon=True).start()

    def stop(self, query_id: int) -> dict[str, Any]:
        with self._guard:
            stop = self._stops.get(query_id)
        if stop is not None:
            stop.set()
        query = self.store.get_query(query_id)
        if query["status"] in {"queued", "running", "waiting"}:
            self.store.update_query(query_id, status="stopped", stopped_at=now_iso(), next_run_at=None, progress="остановлено вручную")
        return self.store.get_query(query_id)

    def resume_open(self, max_running: int = 2) -> None:
        started = 0
        for query in self.store.list_queries():
            if query["status"] not in {"queued", "running", "waiting"} or query["checks_done"] >= query["checks_planned"]:
                continue
            if started >= max_running:
                self.store.update_query(
                    int(query["id"]),
                    status="stopped",
                    stopped_at=now_iso(),
                    next_run_at=None,
                    progress="остановлено: на сервере уже максимум проверок",
                )
                continue
            self.start(int(query["id"]))
            started += 1

    def _loop(self, query_id: int, stop: threading.Event) -> None:
        while not stop.is_set():
            query = self.store.get_query(query_id)
            if query["status"] == "stopped":
                return
            if stop.is_set():
                return
            if query["checks_done"] >= query["checks_planned"]:
                self.store.update_query(query_id, status="finished", next_run_at=None, progress="все проверки выполнены")
                return
            self.store.update_query(query_id, status="running", next_run_at=None, last_error=None)
            try:
                changes = self._check(query_id, stop)
            except Exception as exc:
                if stop.is_set():
                    self.store.update_query(query_id, status="stopped", stopped_at=now_iso(), progress="остановлено вручную")
                    return
                self.store.update_query(query_id, status="error", last_error=str(exc), progress="проверка прервана")
                return
            if stop.is_set() or self.store.get_query(query_id)["status"] == "stopped":
                self.store.update_query(query_id, status="stopped", stopped_at=now_iso(), next_run_at=None, progress="остановлено вручную")
                return
            done = self.store.increment_check(query_id)
            planned = int(query["checks_planned"])
            if done >= planned:
                self.store.update_query(
                    query_id,
                    status="finished",
                    next_run_at=None,
                    progress=f"проверки закончены, в последней изменилось карточек: {changes}",
                )
                return
            interval = int(query["interval_seconds"])
            next_run = datetime.now(QYZYLORDA) + timedelta(seconds=interval)
            self.store.update_query(
                query_id,
                status="waiting",
                next_run_at=next_run.isoformat(timespec="seconds"),
                progress=f"следующая проверка в {next_run.strftime('%H:%M')}, изменений сейчас: {changes}",
            )
            if stop.wait(interval):
                self.store.update_query(query_id, status="stopped", stopped_at=now_iso(), next_run_at=None, progress="остановлено вручную")
                return

    def _check(self, query_id: int, stop: threading.Event) -> int:
        started = time.perf_counter()
        query = self.store.get_query(query_id)
        with self._guard:
            primed = query_id in self._primed
            self._primed.discard(query_id)
        if primed:
            saved = self.store.detail(query_id)["cards"]
            cards = [
                {
                    "product_id": card["product_id"],
                    "title": card["title"],
                    "stock_city": card.get("stock_city"),
                    "cities": card.get("cities"),
                }
                for card in saved
            ]
            found = {"cards": cards, "total": query.get("search_total"), "error": None}
        else:
            self.store.update_query(query_id, progress="ищу карточки сразу по всем городам")
            found = search_cards(self.client, query["name"], query["city_id"])
            if found["error"]:
                raise RuntimeError(found["error"])
            self.store.update_query(query_id, search_total=found["total"])
            cards = found["cards"]
        cards = found["cards"]
        if not cards:
            self.store.update_query(query_id, progress="по этому наименованию карточек нет")
            return 0
        changes = 0
        failures = 0
        prepared: list[tuple[dict[str, Any], int, str]] = []
        for card in cards:
            card_id = self.store.upsert_card(query_id, card)
            stock_city = card.get("stock_city") if isinstance(card.get("stock_city"), str) and card["stock_city"].isdigit() else "750000000"
            prepared.append((card, card_id, stock_city))
        lane = RequestLane(self.client)
        done_count = 0
        counter = threading.Lock()

        def read_card(item: tuple[dict[str, Any], int, str]) -> tuple[int, dict[str, Any] | None]:
            nonlocal done_count
            card, card_id, stock_city = item
            if stop.is_set():
                return card_id, None
            snapshot = snapshot_card(lane, card["product_id"], stock_city)
            with counter:
                done_count += 1
                current = done_count
            self.store.update_query(query_id, progress=f"карточка {current} из {len(prepared)}: {card['title']}")
            if not snapshot.get("ok"):
                return card_id, None
            return card_id, snapshot

        with ThreadPoolExecutor(max_workers=min(READ_PARALLEL, len(prepared))) as pool:
            for card_id, snapshot in pool.map(read_card, prepared):
                if stop.is_set():
                    break
                if snapshot is None:
                    failures += 1
                    continue
                if self.store.save_if_changed(card_id, query_id, snapshot):
                    changes += 1
        shown = len(cards)
        total = found["total"] if isinstance(found["total"], int) else shown
        extra = f" В выдаче {total}, в обзор вошли {shown}." if total > shown else ""
        problem = f" Не прочитано карточек: {failures}." if failures else ""
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        self.store.update_query(
            query_id,
            progress=f"проверка прочитана, изменений: {changes}.{extra}{problem}",
            last_thousand_ms=thousand_card_ms(elapsed_ms, shown),
        )
        return changes
