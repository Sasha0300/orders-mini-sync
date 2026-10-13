import json
import os
import re
import time
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse


class UTF8JSONResponse(JSONResponse):
    def render(self, content):
        return json.dumps(content, ensure_ascii=False).encode("utf-8")


SOURCE_URL = os.environ.get("SOURCE_URL", "https://test.ooobs.ru/api/orders")
CACHE_TTL = int(os.environ.get("CACHE_TTL", "300"))

app = FastAPI(title="orders-mini-sync")

_cache = {"result": None, "fetched_at": 0.0}


def normalize_phone(raw):
    """Return phone as '+7xxxxxxxxxx' or None when it cannot be reduced to
    an 11-digit RU number."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    return None


def parse_ts(value):
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def amount_num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def amount_out(value):
    num = amount_num(value)
    return int(num) if num == int(num) else num


def build_result(orders):
    best = {}
    invalid_dropped = 0
    for order in orders:
        phone = normalize_phone(order.get("phone"))
        if phone is None:
            invalid_dropped += 1
            continue
        client = {
            "name": order.get("name") or "",
            "phone": phone,
            "email": order.get("email") or "",
            "amount": amount_out(order.get("amount")),
        }
        rank = (amount_num(order.get("amount")), parse_ts(order.get("created_at")))
        current = best.get(phone)
        if current is None or rank > current[0]:
            best[phone] = (rank, client)

    clients = [entry[1] for _, entry in sorted(best.items())]
    received = len(orders)
    return {
        "clients": clients,
        "stats": {
            "received": received,
            "unique": len(clients),
            "duplicates_merged": received - invalid_dropped - len(clients),
            "invalid_dropped": invalid_dropped,
        },
    }


async def get_result(force=False):
    now = time.time()
    if force or _cache["result"] is None or now - _cache["fetched_at"] > CACHE_TTL:
        async with httpx.AsyncClient(timeout=20) as http:
            resp = await http.get(SOURCE_URL)
            resp.raise_for_status()
            orders = resp.json()
        if not isinstance(orders, list):
            raise ValueError("unexpected /api/orders payload")
        _cache["result"] = build_result(orders)
        _cache["fetched_at"] = now
    return _cache["result"]


@app.get("/")
async def root():
    return {"service": "orders-mini-sync", "result": "/result", "sync": "/sync"}


@app.get("/result")
async def result():
    return UTF8JSONResponse(await get_result())


@app.api_route("/sync", methods=["GET", "POST"])
async def sync():
    return UTF8JSONResponse(await get_result(force=True))
