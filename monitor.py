"""BIG W Pokemon TCG monitor.

Polls BIG W's search API for Pokemon TCG products sold by BIG W itself (no
marketplace sellers), posts Discord alerts for new listings and restocks,
and keeps a single "currently available" board message up to date.

Standard library only, so it runs on a bare GitHub Actions runner.

Usage:
    python monitor.py               # normal run (used by the workflow)
    python monitor.py --dry-run     # fetch + diff, print payloads, send nothing, save nothing
    python monitor.py --test-alert  # send one sample alert so you can see the format

Environment:
    DISCORD_WEBHOOK_URL        required  channel that receives alerts
    DISCORD_BOARD_WEBHOOK_URL  optional  channel for the live board (defaults to the alert webhook)
    DISCORD_PING               optional  mention added to alerts, e.g. <@123> or <@&456>
    INCLUDE_ACCESSORIES        optional  "true" to also track sleeves/binders/deck boxes
    BIGW_PROXY                 optional  proxy for BIG W requests: host:port:user:pass (or host:port / full URL)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import secrets
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------- config

SITE = "https://www.bigw.com.au"
API_URL = "https://api.bigw.com.au/search/v1/search"
API_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/plain, */*",
    "origin": SITE,
    "referer": SITE + "/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    ),
}
# "Trading Cards" category. Everything BIG W itself sells there is ~2 pages; Pokemon is
# picked out client-side so items filed under "Trading Card Cases & Accessories" or the
# generic "Trading Cards" category are still caught.
CATEGORY_ID = "6815102"
SOLD_BY = "BIG W"            # server-side filter: excludes Marketplace sellers
PER_PAGE = 48
PRODUCT_ATTRIBUTES = [
    "attributes.collectable", "attributes.deliverable", "attributes.listingStatus",
    "attributes.maxQuantity", "information.name", "information.brand",
    "information.categories", "information.media.image", "fulfilment.preorder",
    "fulfilment.delivery", "fulfilment.collection", "fulfilment.productChannel",
    "prices.NAT", "identifiers", "promoLabels.NAT",
]
BOARD_URL = SITE + "/toys/trading-cards/pokemon-trading-cards/c/681510201?soldBy=BIG%20W"

ACCESSORY_CATEGORY = "Trading Card Cases & Accessories"
ACCESSORY_RE = re.compile(
    r"\b(sleeves?|deck ?box|deck protector|portfolio|binder|album|toploader|top loader|"
    r"playmat|play mat|card case|storage box|grading)\b",
    re.I,
)
# BIG W's default per-order cap; only show limits tighter than this.
DEFAULT_MAX_QTY = 24

STATUS_STYLE = {
    # status:      (label,       colour,   emoji)
    "preorder":    ("Pre-order", 0x8B5CF6, "🟣"),
    "in_stock":    ("In stock",  0x22C55E, "🟢"),
    "unavailable": ("Sold out",  0x6B7280, "⚫"),
}
AVAILABLE = {"preorder", "in_stock"}

# --------------------------------------------------------------------------- HTTP


def proxy_url(raw: str) -> str | None:
    """Turn BIGW_PROXY into a proxy URL.

    Accepts host:port:user:pass (the usual proxy-provider export), host:port,
    or a full URL such as http://user:pass@host:port.
    """
    raw = raw.strip()
    if not raw:
        return None
    if "://" in raw:
        return raw
    parts = raw.split(":")
    if len(parts) == 2:
        host, port = parts
        return f"http://{host}:{port}"
    if len(parts) >= 4:
        host, port, user = parts[0], parts[1], parts[2]
        password = ":".join(parts[3:])  # passwords may contain ':'
        q = lambda s: urllib.parse.quote(s, safe="")
        return f"http://{q(user)}:{q(password)}@{host}:{port}"
    raise ValueError("BIGW_PROXY must be host:port:user:pass, host:port, or a full proxy URL")


_BIGW_OPENER: urllib.request.OpenerDirector | None = None


def bigw_opener() -> urllib.request.OpenerDirector:
    """Opener for BIG W requests: goes through BIGW_PROXY when set. Discord traffic never uses the proxy."""
    global _BIGW_OPENER
    if _BIGW_OPENER is None:
        raw = os.environ.get("BIGW_PROXY", "")
        if raw.strip():
            # Shape only, never the value, so a bad paste can be diagnosed from the run log.
            s = raw.strip()
            print(f"BIGW_PROXY shape: {len(s.split(':'))} ':'-separated parts, {len(s)} chars, "
                  f"scheme={'yes' if '://' in s else 'no'}, lines={len(s.splitlines())}, "
                  f"inner whitespace={'yes' if any(c.isspace() for c in s) else 'no'}")
        url = proxy_url(raw)
        handlers = [urllib.request.ProxyHandler({"http": url, "https": url})] if url else []
        _BIGW_OPENER = urllib.request.build_opener(*handlers)
        if url:
            print(f"Using proxy {url.rsplit('@', 1)[-1]} for BIG W requests")
    return _BIGW_OPENER


def http_json(url: str, body: dict | None, headers: dict, method: str = "POST", retries: int = 3,
              opener: urllib.request.OpenerDirector | None = None):
    data = json.dumps(body).encode() if body is not None else None
    open_ = opener.open if opener else urllib.request.urlopen
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with open_(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, (_safe_json(raw) if raw else None)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                if attempt < retries:
                    wait = float(e.headers.get("Retry-After") or 2 * attempt)
                    time.sleep(wait)
                    continue
            return e.code, _safe_json(e.read())
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
            time.sleep(2 * attempt)
    raise RuntimeError("unreachable")


def _safe_json(raw: bytes):
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")[:500]


# --------------------------------------------------------------------------- BIG W API


def session_id() -> str:
    # The website sends a random 21-char id per browser; any value of that shape works.
    alphabet = string.ascii_letters + string.digits + "_-"
    return "".join(secrets.choice(alphabet) for _ in range(21))


def fetch_products(include_accessories: bool) -> dict[str, dict]:
    products: dict[str, dict] = {}
    sid = session_id()
    for page in range(20):  # pages are 0-based; guard against runaway paging
        body = {
            "format": "1",
            "clientId": "web",
            "sessionId": sid,
            "page": page,
            "perPage": PER_PAGE,
            "sort": "relevance",
            "text": "",
            "category": CATEGORY_ID,
            "filter": {"inStock": False, "soldBy": [SOLD_BY]},
            "include": {"facets": False, "suggestions": False, "sponsored": False,
                        "productAttributes": PRODUCT_ATTRIBUTES},
        }
        status, data = http_json(API_URL, body, API_HEADERS, opener=bigw_opener())
        if status != 200 or not isinstance(data, dict) or "organic" not in data:
            raise RuntimeError(f"BIG W search failed: HTTP {status}: {str(data)[:300]}")
        organic = data["organic"]
        for hit in organic.get("results") or []:
            p = normalise(hit)
            if p and keep(p, include_accessories):
                products[p["sku"]] = p
        if page + 1 >= int(organic.get("pageCount") or 0):
            break
    return products


def fix_text(s: str) -> str:
    # BIG W's index mangles "é" into U+FFFD ("Pok�mon").
    return (s or "").replace("Pok�mon", "Pokémon").replace("�", "é").strip()


def plain(s: str) -> str:
    return s.lower().replace("é", "e")


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", plain(s)).strip("-") or "item"


def keep(p: dict, include_accessories: bool) -> bool:
    if "pokemon" not in plain(p["title"]) and plain(p["brand"]) != "pokemon":
        return False
    if p["accessory"]:
        return include_accessories
    # Sealed product must be official Pokemon-branded; drops knock-offs like
    # "Jenjo Games Pokemon Cards" that BIG W drop-ships.
    return plain(p["brand"]) == "pokemon"


def normalise(hit: dict) -> dict | None:
    ids = hit.get("identifiers") or {}
    sku = ids.get("articleId")
    if not sku:
        return None
    info = hit.get("information") or {}
    attrs = hit.get("attributes") or {}
    ful = hit.get("fulfilment") or {}
    nat = (hit.get("prices") or {}).get("NAT") or {}

    title = fix_text(info.get("name", ""))
    cats = [c.get("name", "") for c in info.get("categories") or []]
    accessory = ACCESSORY_CATEGORY in cats or bool(ACCESSORY_RE.search(title))

    pre = ful.get("preorder")
    if pre and hit.get("stock"):
        status = "preorder"
    elif hit.get("stock"):
        status = "in_stock"   # BIG W shows "Add to cart" only when stock is true
    else:
        status = "unavailable"

    release = None
    if isinstance(pre, dict):
        release = (pre.get("releaseDate") or pre.get("availableDate") or pre.get("date")
                   or pre.get("startDate"))

    where = []
    if (ful.get("delivery") or {}).get("standard") or attrs.get("deliverable"):
        where.append("Delivery")
    if (ful.get("collection") or {}).get("pickup"):
        where.append("Pick up")
    if attrs.get("listingStatus") == "LISTEDSELLABLE":
        where.append("In stores")

    price_c = (nat.get("price") or {}).get("cents")
    was_c = (nat.get("wasPrice") or {}).get("cents")
    if not was_c and (nat.get("saving") or {}).get("cents") and price_c:
        was_c = price_c + nat["saving"]["cents"]

    img = (info.get("media") or {}).get("image") or {}
    img_path = img.get("large") or img.get("medium") or img.get("small") or ""
    max_qty = attrs.get("maxQuantity")

    return {
        "sku": str(sku),
        "title": title,
        "brand": fix_text((info.get("brand") or {}).get("name", "")),
        "url": f"{SITE}/product/{slugify(title)}/p/{sku}",
        "image": (SITE + img_path) if img_path.startswith("/") else img_path,
        "price": price_c / 100 if price_c is not None else 0.0,
        "was_price": was_c / 100 if was_c else None,
        "deal": (nat.get("label") or {}).get("text"),
        "status": status,
        "where": where,
        "limit": max_qty if isinstance(max_qty, int) and 0 < max_qty < DEFAULT_MAX_QTY else None,
        "release": release,
        "accessory": accessory,
    }


# --------------------------------------------------------------------------- formatting


def money(v: float | None) -> str:
    if v is None:
        return "—"
    return f"${v:,.0f}" if float(v).is_integer() else f"${v:,.2f}"


def short_title(title: str) -> str:
    for prefix in ("Pokemon TCG: ", "Pokémon TCG: ", "Pokemon TCG - ", "Pokémon TCG - ",
                   "Pokemon Trading Card Game - ", "Pokemon TCG ", "Pokémon TCG "):
        if title.startswith(prefix):
            return title[len(prefix):]
    return title


def ts(iso: str | None, style: str = "D") -> str | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<t:{int(dt.timestamp())}:{style}>"


def alert_embed(p: dict, kind: str) -> dict:
    label, colour, emoji = STATUS_STYLE[p["status"]]
    heading = {"new": "New listing", "restock": "Back in stock"}[kind]

    price = money(p["price"])
    if p["was_price"]:
        price += f"  ~~{money(p['was_price'])}~~"
    if p.get("deal"):
        price += f"  ({p['deal'].title()})"

    fields = [
        {"name": "Price", "value": price, "inline": True},
        {"name": "Status", "value": f"{emoji} {label}", "inline": True},
        {"name": "Limit", "value": f"{p['limit']} per customer" if p["limit"] else "—", "inline": True},
    ]
    rel = ts(p["release"], "D")
    if rel:
        fields.append({"name": "Release", "value": f"{rel} ({ts(p['release'], 'R')})", "inline": True})
    if p["where"]:
        fields.append({"name": "Available", "value": " · ".join(p["where"]), "inline": True})

    embed = {
        "author": {"name": f"BIG W  •  {heading}"},
        "title": p["title"][:256],
        "url": p["url"],
        "color": colour,
        "fields": fields,
        "footer": {"text": f"Article {p['sku']}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if p["image"]:
        embed["thumbnail"] = {"url": p["image"]}
    return embed


def board_embeds(products: dict[str, dict]) -> list[dict]:
    now = int(time.time())
    sections = [("preorder", "Pre-orders"), ("in_stock", "In stock")]
    live = [p for p in products.values() if p["status"] in AVAILABLE]

    header = {
        "title": "BIG W — Pokémon TCG available now",
        "url": BOARD_URL,
        "color": 0x0046BE,
        "description": (
            f"**{len(live)}** products live"
            + "".join(
                f"  ·  {STATUS_STYLE[s][2]} {sum(1 for p in live if p['status'] == s)}"
                for s, _ in sections
            )
            + f"\nLast checked <t:{now}:R>"
        ),
    }
    embeds = [header]
    for status, _ in sections:
        items = sorted(
            (p for p in live if p["status"] == status),
            key=lambda p: (p["release"] or "", p["title"]) if status == "preorder" else (-p["price"], p["title"]),
        )
        embeds.extend(board_card(p) for p in items)

    if len(embeds) == 1:
        header["description"] += "\n\nNothing available right now."
    return embeds


def board_card(p: dict) -> dict:
    """One compact card per product: image on the right, colour = status."""
    label, colour, emoji = STATUS_STYLE[p["status"]]
    price = f"**{money(p['price'])}**"
    if p["was_price"]:
        price += f" ~~{money(p['was_price'])}~~"
    line1 = [price, f"{emoji} {label}"]
    rel = ts(p["release"], "D")
    if rel:
        line1.append(f"out {rel}")
    line2 = []
    if p["limit"]:
        line2.append(f"Limit {p['limit']}")
    if p["where"]:
        line2.append(" + ".join(p["where"]))
    card = {
        "title": short_title(p["title"])[:256],
        "url": p["url"],
        "color": colour,
        "description": " · ".join(line1) + ("\n" + " · ".join(line2) if line2 else ""),
    }
    if p["image"]:
        card["thumbnail"] = {"url": p["image"]}
    return card


# --------------------------------------------------------------------------- Discord

DISCORD_HEADERS = {"content-type": "application/json", "user-agent": "bigw-tcg-monitor"}


def webhook_parts(url: str) -> str:
    return url.split("?")[0].rstrip("/")


def discord_send(webhook: str, payload: dict) -> str | None:
    url = webhook_parts(webhook) + "?wait=true"
    status, data = http_json(url, payload, DISCORD_HEADERS)
    if status not in (200, 204):
        raise RuntimeError(f"Discord send failed: HTTP {status}: {data}")
    return (data or {}).get("id")


def discord_edit(webhook: str, message_id: str, payload: dict) -> bool:
    url = f"{webhook_parts(webhook)}/messages/{message_id}"
    status, data = http_json(url, payload, DISCORD_HEADERS, method="PATCH")
    if status == 404:
        return False
    if status != 200:
        raise RuntimeError(f"Discord edit failed: HTTP {status}: {data}")
    return True


def discord_delete(webhook: str, message_id: str) -> None:
    url = f"{webhook_parts(webhook)}/messages/{message_id}"
    status, data = http_json(url, None, {"user-agent": "bigw-tcg-monitor"}, method="DELETE")
    if status not in (204, 404):
        raise RuntimeError(f"Discord delete failed: HTTP {status}: {data}")


def send_alerts(webhook: str, ping: str, alerts: list[tuple[dict, str]]) -> None:
    # Discord allows 10 embeds per message; batch so a big drop is one or two messages.
    for i in range(0, len(alerts), 10):
        batch = alerts[i:i + 10]
        payload = {
            "username": "BIG W Monitor",
            "embeds": [alert_embed(p, kind) for p, kind in batch],
            "allowed_mentions": {"parse": ["users", "roles"]},
        }
        if ping:
            payload["content"] = ping
        discord_send(webhook, payload)
        time.sleep(1)


def update_board(webhook: str, state: dict, products: dict[str, dict]) -> None:
    embeds = board_embeds(products)
    # Discord caps a message at 10 embeds: header + 9 cards in the first, 10 cards in each after.
    chunks = [embeds[i:i + 10] for i in range(0, len(embeds), 10)]
    payloads = [{"embeds": c, "allowed_mentions": {"parse": []}} for c in chunks]
    ids = state.get("board_message_ids") or []

    if len(ids) == len(payloads):
        ok = True
        for msg_id, payload in zip(ids, payloads):
            if not discord_edit(webhook, msg_id, payload):
                ok = False  # someone deleted a board message
                break
            time.sleep(0.5)
        if ok:
            state["board_message_ids"] = ids
            return

    # Message count changed (or a message vanished): repost the whole board so it stays contiguous.
    for msg_id in ids:
        discord_delete(webhook, msg_id)
        time.sleep(0.5)
    new_ids = []
    for payload in payloads:
        new_ids.append(discord_send(webhook, {**payload, "username": "BIG W Monitor"}))
        time.sleep(0.5)
    state["board_message_ids"] = new_ids
    print(f"Posted board as {len(new_ids)} message(s) {new_ids} — pin the first one in Discord.")


# --------------------------------------------------------------------------- state + diff


def load_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(state: dict, path: Path) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def diff(prev: dict[str, dict], current: dict[str, dict]) -> list[tuple[dict, str]]:
    alerts = []
    for sku, p in current.items():
        old = prev.get(sku)
        if old is None:
            if p["status"] in AVAILABLE:
                alerts.append((p, "new"))
        elif p["status"] in AVAILABLE and old.get("status") not in AVAILABLE:
            alerts.append((p, "restock"))
    return alerts


def merge(prev: dict[str, dict], current: dict[str, dict]) -> dict[str, dict]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    merged = {}
    for sku, p in current.items():
        # No per-run timestamps here, so state.json only changes (and gets committed) on real changes.
        merged[sku] = {**p, "first_seen": prev.get(sku, {}).get("first_seen", now)}
    for sku, old in prev.items():
        if sku not in current:
            # Dropped out of results (delisted, or no longer sold by BIG W): treat as sold out.
            merged[sku] = {**old, "status": "unavailable"}
    return merged


# --------------------------------------------------------------------------- timing

INTERVAL_MIN = 15      # check on every quarter hour...
OFFSET_SEC = 1         # ...one second past it: 4:00:01, 4:15:01, ...


def slot_for(now: float, cron_minute: int | None) -> float:
    """The check time this run is responsible for.

    With the cron minute that triggered the run (e.g. 53), the slot is the first quarter hour
    after the most recent firing of that minute, so a run GitHub started late still knows which
    slot it was meant for. Without it, fall back to the next quarter hour.
    """
    period = INTERVAL_MIN * 60
    if cron_minute is None:
        return (now - OFFSET_SEC) // period * period + period + OFFSET_SEC
    hour = now // 3600 * 3600
    fired = hour + cron_minute * 60
    if fired > now:
        fired -= 3600
    return (fired // period) * period + period + OFFSET_SEC


def wait_for_slot(cron_minute: int | None) -> None:
    now = time.time()
    slot = slot_for(now, cron_minute)
    wait = slot - now
    when = datetime.fromtimestamp(slot, timezone.utc).strftime("%H:%M:%S UTC")
    if wait <= 0:
        # GitHub started us after the slot; check now rather than skip it.
        print(f"Started {-wait:.0f}s after the {when} slot; checking immediately.")
        return
    print(f"Waiting {wait:.1f}s for {when}")
    while (remaining := slot - time.time()) > 0:
        time.sleep(min(remaining, 30))


# --------------------------------------------------------------------------- main


LOG_DIR = Path(__file__).with_name("logs")


def load_dotenv(path: Path) -> None:
    """Minimal .env loader for local runs (KEY=VALUE lines). Real env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def log_csv(name: str, header: list[str], row: list) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / name
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def log_alerts(alerts: list[tuple[dict, str]], detected: datetime) -> None:
    for p, kind in alerts:
        log_csv("alerts.csv", ["detected_utc", "kind", "sku", "title", "status", "price"],
                [detected.isoformat(timespec="seconds"), kind, p["sku"], p["title"], p["status"], p["price"]])


def process(state: dict, current: dict[str, dict], cfg: dict, dry_run: bool,
            log: bool = False) -> tuple[dict, list[tuple[dict, str]]]:
    """Diff a fetch against state, send alerts, return (merged products, alerts)."""
    prev = state.get("products", {})
    first_run = not prev and not state.get("seeded")
    alerts = [] if first_run else diff(prev, current)
    merged = merge(prev, current)
    if first_run:
        print("First run: seeding state without alerts.")
    for p, kind in alerts:
        print(f"[{kind}] {p['title']} — {money(p['price'])} — {p['status']}")
    if log and alerts:
        log_alerts(alerts, datetime.now(timezone.utc))
    if alerts and not dry_run:
        send_alerts(cfg["alert_hook"], cfg["ping"], alerts)
    state["seeded"] = True
    return merged, alerts


POLL_HEADER = ["utc", "ok", "ms", "products", "available", "error"]


def fetch_logged(cfg: dict) -> dict[str, dict]:
    t0 = time.time()
    started = datetime.now(timezone.utc)
    try:
        current = fetch_products(cfg["include_acc"])
    except Exception as e:
        if cfg.get("log_polls"):
            log_csv("polls.csv", POLL_HEADER,
                    [started.isoformat(timespec="seconds"), 0, int((time.time() - t0) * 1000), "", "", str(e)[:200]])
        raise
    ms = int((time.time() - t0) * 1000)
    avail = sum(p["status"] in AVAILABLE for p in current.values())
    if cfg.get("log_polls"):
        log_csv("polls.csv", POLL_HEADER, [started.isoformat(timespec="seconds"), 1, ms, len(current), avail, ""])
    print(f"[{started.strftime('%H:%M:%S.%f')[:-3]} UTC] Fetched {len(current)} products "
          f"({avail} available) in {ms} ms")
    return current


def run_once(cfg: dict, state_path: Path, dry_run: bool) -> int:
    current = fetch_logged(cfg)
    state = load_state(state_path) or {"products": {}}
    if not current and state.get("products"):
        print("API returned no products; skipping this run to avoid false sold-out states.")
        return 0
    merged, _ = process(state, current, cfg, dry_run)
    if dry_run:
        for p in sorted(merged.values(), key=lambda p: (p["status"], p["title"])):
            print(f"  {STATUS_STYLE[p['status']][2]} {p['sku']}  {money(p['price']):>8}  {p['title']}"
                  f"  [{' + '.join(p['where'])}]")
        print(json.dumps({"embeds": board_embeds(merged)}, indent=2, ensure_ascii=False))
        return 0
    state["products"] = merged
    update_board(cfg["board_hook"], state, merged)
    save_state(state, state_path)
    return 0


def run_loop(cfg: dict, state_path: Path, interval: float, dry_run: bool) -> int:
    """Poll forever. Board is refreshed when something changes, or at most once a minute."""
    board_every = 60
    state = load_state(state_path) or {"products": {}}
    last_board = 0.0
    failures = 0
    print(f"Loop mode: polling every ~{interval:g}s (±20%), state in {state_path.name}. Ctrl-C to stop.")
    while True:
        t0 = time.time()
        try:
            current = fetch_logged(cfg)
            failures = 0
            if not current and state.get("products"):
                print("  empty result; ignoring")
            else:
                before = json.dumps(state.get("products", {}), sort_keys=True)
                ids_before = state.get("board_message_ids")
                merged, _ = process(state, current, cfg, dry_run, log=True)
                state["products"] = merged
                changed = json.dumps(merged, sort_keys=True) != before
                if not dry_run and (changed or time.time() - last_board >= board_every):
                    update_board(cfg["board_hook"], state, merged)
                    last_board = time.time()
                if not dry_run and (changed or state.get("board_message_ids") != ids_before):
                    save_state(state, state_path)
            delay = interval * random.uniform(0.8, 1.2) - (time.time() - t0)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            failures += 1
            delay = min(300, interval * 2 ** failures)  # back off hard on errors / 429s
            print(f"  error #{failures}: {e} — backing off {delay:.0f}s", file=sys.stderr)
        time.sleep(max(1.0, delay))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        # UTF-8 for emoji on Windows consoles; line buffering so loop output shows up live in logs.
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="don't send to Discord or write state")
    ap.add_argument("--test-alert", action="store_true", help="send one sample alert and exit")
    ap.add_argument("--at-quarter", action="store_true",
                    help="sleep until the next :00:01/:15:01/:30:01/:45:01 before checking")
    ap.add_argument("--schedule", default="",
                    help='cron expression that triggered the run (GitHub passes e.g. "53 * * * *")')
    ap.add_argument("--loop", type=float, metavar="SECONDS",
                    help="poll continuously every SECONDS (±20%%), logging to logs/")
    ap.add_argument("--state", default="state.json", help="state file (use a separate one for local trials)")
    ap.add_argument("--env-file", default=".env", help="local file with DISCORD_WEBHOOK_URL=... etc.")
    args = ap.parse_args()

    here = Path(__file__).parent
    load_dotenv(here / args.env_file)
    alert_hook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    cfg = {
        "alert_hook": alert_hook,
        "board_hook": os.environ.get("DISCORD_BOARD_WEBHOOK_URL", "").strip() or alert_hook,
        "ping": os.environ.get("DISCORD_PING", "").strip(),
        "include_acc": os.environ.get("INCLUDE_ACCESSORIES", "").lower() in ("1", "true", "yes"),
        "log_polls": bool(args.loop),
    }
    state_path = Path(args.state) if Path(args.state).is_absolute() else here / args.state

    if not alert_hook and not args.dry_run:
        print("DISCORD_WEBHOOK_URL is not set (env var or .env file)", file=sys.stderr)
        return 2

    if args.test_alert:
        current = fetch_logged(cfg)
        sample = next((p for p in current.values() if p["status"] in AVAILABLE), None)
        if not sample:
            print("Nothing available to use as a sample.")
            return 1
        send_alerts(alert_hook, cfg["ping"], [(sample, "new")])
        print(f"Sent test alert for {sample['title']}")
        return 0

    if args.loop:
        if args.loop < 5:
            print("--loop below 5 seconds is asking to get rate-limited; use 5 or more.", file=sys.stderr)
            return 2
        try:
            return run_loop(cfg, state_path, args.loop, args.dry_run)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0

    if args.at_quarter:
        first = args.schedule.split()[0] if args.schedule.strip() else ""
        wait_for_slot(int(first) if first.isdigit() else None)
    return run_once(cfg, state_path, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
