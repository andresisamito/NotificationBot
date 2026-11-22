"""Alerta Booking Bratislava -> Telegram. Solo stdlib."""
import html, json, math, os, sys, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta

CHECK_IN, CHECK_OUT = "2026-12-29", "2027-01-03"
NIGHTS = 5
MAX_TOTAL = 500          # 100 EUR/noche
MIN_RATING = 6.5
MAX_KM = 2.0
CENTER = (48.1437, 17.1077)  # Hlavne namestie
ALLOWED_TYPES = {"hotel", "apartment", "aparthotel"}
BUDGET_USD = 4.6         # margen bajo los 5 $ gratis de Apify por ciclo
DEFAULT_RUN_COST = 0.06  # se recalcula con el coste real de cada busqueda
MIN_INTERVAL_H, MAX_INTERVAL_H = 1, 24
LAST_CHECK = datetime(2026, 12, 28, 23, 59, tzinfo=timezone.utc)
STATE_FILE = "state.json"

APIFY = os.environ["APIFY_TOKEN"]
TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
FORCE = os.environ.get("FORCE") == "true"


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"last_run": None, "seen": {}, "run_cost": DEFAULT_RUN_COST}


def apify_usage():
    """Devuelve (gasto del ciclo en USD, fin del ciclo)."""
    with urllib.request.urlopen(f"https://api.apify.com/v2/users/me/limits?token={APIFY}", timeout=30) as r:
        d = json.load(r)["data"]
    end = datetime.fromisoformat(d["monthlyUsageCycle"]["endAt"].replace("Z", "+00:00"))
    return float(d["current"]["monthlyUsageUsd"]), end


def km(lat, lng):
    x = math.radians(lng - CENTER[1]) * math.cos(math.radians((lat + CENTER[0]) / 2))
    y = math.radians(lat - CENTER[0])
    return 6371 * math.hypot(x, y)


def scrape(prop_type):
    inp = {
        "search": "Bratislava", "checkIn": CHECK_IN, "checkOut": CHECK_OUT,
        "adults": 2, "rooms": 1, "currency": "EUR", "language": "es",
        "minScore": str(MIN_RATING), "sortBy": "price", "minMaxPrice": "0-100",
        "propertyType": prop_type, "maxItems": 15,
    }
    qs = urllib.parse.urlencode({
        "token": APIFY, "maxTotalChargeUsd": "0.15", "memory": "1024", "timeout": "280",
        "fields": "hotelId,name,type,price,rating,url,location,rooms",
    })
    req = urllib.request.Request(
        f"https://api.apify.com/v2/acts/voyager~booking-scraper/run-sync-get-dataset-items?{qs}",
        data=json.dumps(inp).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


def has_deal(item):
    for room in item.get("rooms") or []:
        for opt in room.get("options") or []:
            if opt.get("discount"):
                return True
    return False


def telegram(text):
    data = urllib.parse.urlencode({
        "chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data, timeout=30)


def main():
    now = datetime.now(timezone.utc)
    if now > LAST_CHECK:
        print("Fuera de plazo, nada que hacer."); return
    state = load_state()
    run_cost = state.get("run_cost", DEFAULT_RUN_COST)
    used, cycle_end = apify_usage()
    remaining = BUDGET_USD - used
    if remaining < run_cost:
        print(f"Presupuesto del ciclo agotado ({used:.2f} $). Espero al siguiente ciclo."); return
    horizon_h = (min(cycle_end, LAST_CHECK) - now).total_seconds() / 3600
    interval_h = min(MAX_INTERVAL_H, max(MIN_INTERVAL_H, horizon_h / (remaining / run_cost)))
    print(f"Gastado {used:.2f} $, quedan {remaining:.2f} $, coste/busqueda {run_cost:.3f} $, "
          f"horas restantes {horizon_h:.0f} -> intervalo {interval_h:.1f} h")
    if state["last_run"] and not FORCE:
        if now - datetime.fromisoformat(state["last_run"]) < timedelta(hours=interval_h - 0.25):
            print("Aun no toca."); return

    items = []
    for t in ("Hotels", "Apartments"):
        try:
            items += scrape(t)
        except Exception as e:  # noqa
            print(f"Error en {t}: {e}", file=sys.stderr)

    seen, alerts, current = state["seen"], [], {}
    for it in items:
        try:
            hid = str(it["hotelId"]); price = float(it["price"]); rating = float(it.get("rating") or 0)
            loc = it.get("location") or {}
            dist = km(float(loc["lat"]), float(loc["lng"]))
        except (KeyError, TypeError, ValueError):
            continue
        if (it.get("type") not in ALLOWED_TYPES or price > MAX_TOTAL
                or rating < MIN_RATING or dist > MAX_KM or hid in current):
            continue
        deal = has_deal(it)
        current[hid] = {"price": round(price, 2), "deal": deal, "name": it.get("name")}
        prev = seen.get(hid)
        reason = None
        if prev is None:
            reason = "Nuevo"
        elif price < prev["price"] - 1:
            reason = f"Bajó de {prev['price']:.0f} €"
        elif deal and not prev.get("deal"):
            reason = "Ahora en oferta"
        if reason:
            url = (it.get("url") or "").split("?")[0] + \
                f"?checkin={CHECK_IN}&checkout={CHECK_OUT}&group_adults=2&no_rooms=1&selected_currency=EUR"
            alerts.append(
                f"<b>{reason}</b>{' · OFERTA' if deal and reason != 'Ahora en oferta' else ''}\n"
                f"<a href=\"{html.escape(url)}\">{html.escape(str(it.get('name')))}</a> ({it.get('type')})\n"
                f"{price:.0f} € total · {price / NIGHTS:.0f} €/noche · nota {rating} · {dist:.1f} km")

    try:  # coste real de esta busqueda (media movil)
        after, _ = apify_usage()
        if after > used:
            state["run_cost"] = round(0.7 * run_cost + 0.3 * (after - used), 4)
    except Exception:  # noqa
        pass
    seen.update(current)
    state["seen"], state["last_run"] = seen, now.isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)

    if alerts:
        telegram("Bratislava 29/12–3/01, 2 pers.\n\n" + "\n\n".join(alerts))
    print(f"{len(items)} resultados, {len(current)} cumplen, {len(alerts)} avisos.")


if __name__ == "__main__":
    main()
