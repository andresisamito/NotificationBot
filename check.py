"""Alerta Booking Bratislava -> Telegram. Solo stdlib."""
import html, json, math, os, sys, traceback, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

CHECK_IN, CHECK_OUT = "2026-12-29", "2027-01-03"
NIGHTS = 5
MAX_TOTAL = 500          # 100 EUR/noche
MIN_RATING = 6.5
MAX_KM = 2.0
CENTER = (48.1437, 17.1077)  # Hlavne namestie
ALLOWED_TYPES = {"hotel", "apartment", "aparthotel"}
BUDGET_USD = 4.6         # margen bajo los 5 $ gratis de Apify por ciclo
DEFAULT_RUN_COST = 0.06  # se recalcula con el coste real de cada búsqueda
MIN_INTERVAL_H, MAX_INTERVAL_H = 1, 24
LAST_CHECK = datetime(2026, 12, 28, 23, 59, tzinfo=timezone.utc)
STATE_FILE = "state.json"
TZ = ZoneInfo("Europe/Madrid")
FILTERS = "hotel/apartamento · ≤2 km del casco antiguo · ≤500 € total (100 €/noche) · nota ≥6,5"
HEADER = "Bratislava 29/12–3/01 · 2 pers."

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


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def notify_once(state, key, text):
    """Avisa una sola vez por motivo; se rearma con clear_notice()."""
    flags = state.setdefault("notified", {})
    if not flags.get(key):
        telegram(f"{HEADER}\n\n{text}")
        flags[key] = True
        save_state(state)


def clear_notice(state, key):
    state.setdefault("notified", {}).pop(key, None)


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
    state = load_state()
    if now > LAST_CHECK:
        notify_once(state, "end", "Alerta finalizada: ya no hay más búsquedas (plazo hasta el 28/12). "
                    "Puedes desactivar el workflow en GitHub → Actions.")
        print("Fuera de plazo."); return
    run_cost = state.get("run_cost", DEFAULT_RUN_COST)
    try:
        used, cycle_end = apify_usage()
    except urllib.error.HTTPError as e:
        notify_once(state, "apify_error", f"No puedo buscar: Apify respondió {e.code} al consultar el saldo. "
                    "Revisa el secreto APIFY_TOKEN en GitHub. No volveré a avisar hasta que se arregle.")
        print(f"Error Apify {e.code}"); return
    clear_notice(state, "apify_error")
    remaining = BUDGET_USD - used
    if remaining < run_cost:
        notify_once(state, "budget", f"Sin búsquedas: presupuesto de Apify agotado ({used:.2f} $ de {BUDGET_USD:.2f} $). "
                    f"Se reanudan el {cycle_end.astimezone(TZ):%d/%m} con el nuevo ciclo.")
        print("Presupuesto agotado."); return
    clear_notice(state, "budget")
    horizon_h = (min(cycle_end, LAST_CHECK) - now).total_seconds() / 3600
    interval_h = min(MAX_INTERVAL_H, max(MIN_INTERVAL_H, horizon_h / (remaining / run_cost)))
    print(f"Gastado {used:.2f} $, quedan {remaining:.2f} $, coste/busqueda {run_cost:.3f} $, "
          f"horas restantes {horizon_h:.0f} -> intervalo {interval_h:.1f} h")
    if state["last_run"] and not FORCE:
        if now - datetime.fromisoformat(state["last_run"]) < timedelta(hours=interval_h - 0.25):
            print("Aun no toca."); return

    items, errors = [], []
    for t in ("Hotels", "Apartments"):
        try:
            items += scrape(t)
        except Exception as e:  # noqa
            errors.append(f"{t}: {e}")
            print(f"Error en {t}: {e}", file=sys.stderr)
    if len(errors) == 2:
        notify_once(state, "scrape_error", "La búsqueda en Booking falló (Apify):\n" + html.escape("\n".join(errors)))
        return
    clear_notice(state, "scrape_error")

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
        url = (it.get("url") or "").split("?")[0] + \
            f"?checkin={CHECK_IN}&checkout={CHECK_OUT}&group_adults=2&no_rooms=1&selected_currency=EUR"
        name = html.escape(str(it.get("name")))
        current[hid] = {"price": round(price, 2), "deal": deal, "name": it.get("name"),
                        "line": f"<a href=\"{html.escape(url)}\">{name}</a> · {price:.0f} € · {dist:.1f} km"
                                f"{' · OFERTA' if deal else ''}"}
        prev = seen.get(hid)
        reason = None
        if prev is None:
            reason = "Nuevo"
        elif price < prev["price"] - 1:
            reason = f"Bajó de {prev['price']:.0f} €"
        elif deal and not prev.get("deal"):
            reason = "Ahora en oferta"
        if reason:
            alerts.append(
                f"<b>{reason}</b>{' · OFERTA' if deal and reason != 'Ahora en oferta' else ''}\n"
                f"<a href=\"{html.escape(url)}\">{name}</a> ({it.get('type')})\n"
                f"{price:.0f} € total · {price / NIGHTS:.0f} €/noche · nota {rating} · {dist:.1f} km")

    after = used
    try:  # coste real de esta busqueda (media movil)
        after, _ = apify_usage()
        if after > used:
            state["run_cost"] = round(0.7 * run_cost + 0.3 * (after - used), 4)
    except Exception:  # noqa
        pass
    seen.update({k: {kk: vv for kk, vv in v.items() if kk != "line"} for k, v in current.items()})
    state["seen"], state["last_run"] = seen, now.isoformat()
    save_state(state)

    parts = [f"{HEADER} · búsqueda {now.astimezone(TZ):%d/%m %H:%M}"]
    if alerts:
        parts.append("\n\n".join(alerts))
    elif current:
        parts.append("Sin novedades. Siguen disponibles:\n" + "\n".join(v["line"] for v in current.values()))
    else:
        parts.append(f"Ningún alojamiento cumple los filtros ({len(items)} revisados).")
    nxt = now + timedelta(hours=interval_h)
    parts.append(f"Filtros: {FILTERS}\nPróxima búsqueda: ~{nxt.astimezone(TZ):%d/%m %H:%M} · "
                 f"Apify: {after:.2f} $ de {BUDGET_USD:.2f} $")
    telegram("\n\n".join(parts))
    print(f"{len(items)} resultados, {len(current)} cumplen, {len(alerts)} avisos.")


def run():
    try:
        main()
    except Exception:
        err = traceback.format_exc()[-1500:]
        try:
            telegram(f"{HEADER}\n\nError en la alerta:\n<pre>{html.escape(err)}</pre>")
        finally:
            raise


if __name__ == "__main__":
    run()
