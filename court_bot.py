#!/usr/bin/env python3
"""Court Bot: watches ClubSpark tennis venues for free courts and alerts you on Telegram.

Commands:
  watch   Check venues against VENUES rules below and alert on new or freed-up slots.
          Meant to run every few minutes (see .github/workflows/watch.yml).
  test    One-off check of a specific date and start-time window. Always sends a result.

Environment: TELEGRAM_BOT_TOKEN (required), TELEGRAM_CHAT_ID (optional, auto-detected).
No third-party packages needed.
"""
import argparse
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None
    if os.environ.get("GITHUB_ACTIONS"):
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "curl_cffi"], check=False)
        try:
            from curl_cffi import requests as curl_requests
        except ImportError:
            curl_requests = None

TZ = ZoneInfo("Europe/London")
BASE = "https://clubspark.lta.org.uk"
STATE_FILE = Path(__file__).with_name("state.json")

MIN_MINUTES = 60         # only alert for slots you can book for at least this long
ADVANCE_DAYS = 7         # courts can be booked this many days ahead...
RELEASE_TIME = "20:00"   # ...and the next day is released at this time every day

# EDIT HERE: what to watch. Times are START times (HH:MM, inclusive). Days: Mon..Sun.
VENUES = {
    "burgess": {
        "name": "Burgess Park",
        "slug": "BurgessParkSouthwark",
        "rules": [
            {"days": ["Fri"], "from": "18:00", "to": "20:00"},
            {"days": ["Sat", "Sun"], "from": "10:00", "to": "20:00"},
        ],
        # Courts with 'no light' in their name must finish by this time (winter value).
        "unlit_courts_last_end": "16:00",
    },
    "tanner": {
        "name": "Tanner St Park",
        "slug": "TannerStPark",
        "rules": [
            {"days": ["Sat", "Sun"], "from": "10:00", "to": "15:00"},
        ],
        "unlit_courts_last_end": "16:00",
    },
}
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---------------------------------------------------------------- helpers

def hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def fmt(minutes):
    return "%02d:%02d" % (minutes // 60, minutes % 60)


BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0.0.0 Safari/537.36")


def http_json(url, payload=None, tries=3, extra_headers=None):
    detail = ""
    for i in range(tries):
        try:
            headers = {"User-Agent": BROWSER_UA, "Accept": "application/json, text/javascript, */*; q=0.01",
                       "Accept-Language": "en-GB,en;q=0.9"}
            headers.update(extra_headers or {})
            data = None
            if payload is not None:
                data = json.dumps(payload).encode()
                headers["Content-Type"] = "application/json"
            if data is None and "clubspark" in url and curl_requests is not None:
                # Cloudflare blocks the plain Python TLS fingerprint; impersonate Chrome instead.
                r = curl_requests.get(url, headers=headers, impersonate="chrome", timeout=30)
                if r.status_code != 200:
                    raise RuntimeError("HTTP %s from %s (server=%s): %s" % (
                        r.status_code, urllib.parse.urlparse(url).netloc, r.headers.get("server", "?"),
                        r.text[:200].replace("\n", " ")))
                return r.json()
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except Exception as exc:
            # Only the host is reported, never the full URL (Telegram URLs contain the token).
            detail = "%s %s from %s" % (type(exc).__name__, getattr(exc, "code", ""), urllib.parse.urlparse(url).netloc)
            if isinstance(exc, urllib.error.HTTPError):
                try:
                    detail += " (server=%s): %s" % (exc.headers.get("Server", "?"),
                                                    exc.read(200).decode("utf-8", "replace").replace("\n", " "))
                except Exception:
                    pass
            time.sleep(2 * (i + 1))
    raise RuntimeError("request failed after %d tries: %s" % (tries, detail))


def bookable_until(now):
    """Last date that can be booked right now (the 8pm daily release extends it by one day)."""
    last = now.date() + timedelta(days=ADVANCE_DAYS - 1)
    if now.hour * 60 + now.minute >= hm(RELEASE_TIME):
        last += timedelta(days=1)
    return last


def fetch(slug, start, end):
    url = "%s/v0/VenueBooking/%s/GetVenueSessions?resourceID=&startDate=%s&endDate=%s&roleId=&_=%d" % (
        BASE, slug, start, end, int(time.time() * 1000))
    return http_json(url, extra_headers={"Referer": "%s/%s/Booking/BookByDate" % (BASE, slug),
                                         "X-Requested-With": "XMLHttpRequest"})


def load_data(venue, start, end, fixture=None):
    if fixture:
        with open(fixture, encoding="utf-8") as fh:
            return json.load(fh)
    return fetch(venue["slug"], start, end)


# ---------------------------------------------------------------- core logic

def find_slots(data, venue, windows_for, now, last_date):
    """Return free slots of at least MIN_MINUTES that satisfy the start-time windows.

    ClubSpark marks FREE time as Category 0 sessions (they carry the price in CourtCost per
    interval). Anything else (bookings, coaching, closed) is treated as busy.
    """
    step = data.get("MinimumInterval") or 30
    need = -(-MIN_MINUTES // step)  # ceil: number of intervals in a minimum booking
    unlit_end = hm(venue.get("unlit_courts_last_end", "16:00"))
    now_min = now.hour * 60 + now.minute
    found = []
    for res in data.get("Resources", []):
        court = res.get("Name", "?")
        unlit = "no light" in court.lower() or "no floodlight" in court.lower()
        for day in res.get("Days", []):
            d = datetime.fromisoformat(day["Date"][:10]).date()
            if d < now.date() or d > last_date:
                continue
            wins = windows_for(d)
            if not wins:
                continue
            cost, busy = {}, set()
            for s in day.get("Sessions", []):
                cells = range(s["StartTime"], s["EndTime"], step)
                if s.get("Category") == 0:
                    for t in cells:
                        cost[t] = s.get("CourtCost") or s.get("CostFrom") or 0
                else:
                    busy.update(cells)
            for t in sorted(cost):
                span = [t + i * step for i in range(need)]
                if any(x not in cost or x in busy for x in span):
                    continue
                end = t + need * step
                if d == now.date() and t < now_min + 10:
                    continue
                if not any(a <= t <= b for a, b in wins):
                    continue
                if unlit and end > unlit_end:
                    continue
                found.append({
                    "venue": venue["name"], "slug": venue["slug"], "court": court,
                    "date": d.isoformat(), "start": t, "end": end, "step": step,
                    "price": round(sum(cost[x] for x in span), 2),
                })
    return found


def key(slot):
    return "%s|%s|%s|%d" % (slot["slug"], slot["court"], slot["date"], slot["start"])


def group_runs(slots):
    """Merge consecutive start times on one court into runs, e.g. 07:00-10:00 free."""
    runs = []
    for s in sorted(slots, key=lambda x: x["start"]):
        if runs and s["start"] - runs[-1]["last"] == s["step"]:
            r = runs[-1]
            r["last"], r["end"] = s["start"], s["end"]
            r["pmin"], r["pmax"] = min(r["pmin"], s["price"]), max(r["pmax"], s["price"])
        else:
            runs.append({"start": s["start"], "last": s["start"], "end": s["end"],
                         "pmin": s["price"], "pmax": s["price"]})
    return runs


def build_messages(slots):
    """One Telegram message per venue and date. Returns a list of (text, booking_url)."""
    groups = {}
    for s in slots:
        groups.setdefault((s["venue"], s["slug"], s["date"]), []).append(s)
    messages = []
    for (venue, slug, d), items in sorted(groups.items(), key=lambda kv: (kv[0][2], kv[0][0])):
        kinds = {s.get("kind") for s in items}
        if "freed" in kinds:
            head = "♻️ Freed up (cancellation)"
        elif "released" in kinds:
            head = "\U0001f195 New day released"
        else:
            head = "\U0001f50e Free slots"
        day = datetime.fromisoformat(d).strftime("%a %d %b")
        lines = [head, "\U0001f3be <b>%s</b>, <b>%s</b>" % (html.escape(venue), day)]
        by_court = {}
        for s in items:
            by_court.setdefault(s["court"], []).append(s)
        for court in sorted(by_court):
            for r in group_runs(by_court[court]):
                if r["pmin"] == r["pmax"]:
                    price = "£%.2f" % r["pmin"]
                else:
                    price = "£%.2f-£%.2f" % (r["pmin"], r["pmax"])
                lines.append("• %s: %s-%s free, book %d min from %s" % (
                    html.escape(court), fmt(r["start"]), fmt(r["end"]), MIN_MINUTES, price))
        messages.append(("\n".join(lines), "%s/%s/Booking/BookByDate#?date=%s" % (BASE, slug, d)))
    return messages


# ---------------------------------------------------------------- Telegram

class Telegram:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.discovered = False
        if not dry_run:
            if not self.token:
                raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
            if not self.chat_id:
                self.chat_id = self._discover()
                self.discovered = True

    def _call(self, method, payload):
        try:
            return http_json("https://api.telegram.org/bot%s/%s" % (self.token, method), payload)
        except RuntimeError as exc:
            raise RuntimeError("telegram %s failed: %s" % (method, exc))

    def _discover(self):
        res = self._call("getUpdates", {})
        for update in reversed(res.get("result", [])):
            chat = (update.get("message") or {}).get("chat") or {}
            if chat.get("type") == "private":
                return str(chat["id"])
        raise RuntimeError("No chat found. Open your bot in Telegram, press Start, then re-run.")

    def send(self, text, url=None):
        if self.dry_run:
            print("----- telegram message -----")
            print(text)
            if url:
                print("[button] Open booking page ->", url)
            return
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if url:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": "Open booking page", "url": url}]]}
        self._call("sendMessage", payload)


# ---------------------------------------------------------------- commands

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def cmd_watch(args):
    now = datetime.now(TZ)
    last = bookable_until(now)
    tg = Telegram(args.dry_run)
    state = load_state()
    current = {}
    for venue in VENUES.values():
        def windows_for(d, venue=venue):
            return [(hm(r["from"]), hm(r["to"])) for r in venue["rules"] if DAYS[d.weekday()] in r["days"]]
        data = load_data(venue, now.date().isoformat(), last.isoformat(), args.fixture)
        for s in find_slots(data, venue, windows_for, now, last):
            current[key(s)] = s
    print("%s: %d matching slots (bookable up to %s)" % (now.strftime("%Y-%m-%d %H:%M"), len(current), last))

    if not state.get("initialised"):
        msgs = build_messages(list(current.values()))
        intro = "✅ Court Bot is watching. %d matching slots are free right now." % len(current)
        if msgs:
            intro += " Here are the next few; from now on you only get pinged for NEW or freed-up slots."
        tg.send(intro)
        for text, url in msgs[:4]:
            tg.send(text, url)
        if len(msgs) > 4:
            tg.send("...and %d more dates with free slots." % (len(msgs) - 4))
    else:
        prev = set(state.get("slots", []))
        horizon_prev = state.get("horizon")
        new = [s for k, s in current.items() if k not in prev]
        for s in new:
            s["kind"] = "released" if horizon_prev and s["date"] > horizon_prev else "freed"
        msgs = build_messages(new)
        for text, url in msgs[:6]:
            tg.send(text, url)
        if len(msgs) > 6:
            tg.send("...plus %d more dates with newly free slots." % (len(msgs) - 6))
        print("alerts sent: %d" % min(len(msgs), 6))

    new_state = {"initialised": True, "horizon": last.isoformat(), "slots": sorted(current)}
    if new_state != state and not args.dry_run:
        STATE_FILE.write_text(json.dumps(new_state, indent=1) + "\n", encoding="utf-8")


def cmd_test(args):
    now = datetime.now(TZ)
    day = datetime.fromisoformat(args.date).date() if args.date else now.date() + timedelta(days=5)
    lo, hi = hm(args.start_from), hm(args.start_to)
    names = list(VENUES) if args.venue == "all" else [args.venue]
    tg = Telegram(args.dry_run)
    slots = []
    for name in names:
        if name not in VENUES:
            raise SystemExit("Unknown venue '%s'. Use one of: %s, all" % (name, ", ".join(VENUES)))
        venue = VENUES[name]
        data = load_data(venue, day.isoformat(), day.isoformat(), args.fixture)
        slots += find_slots(data, venue, lambda d: [(lo, hi)], now, day)
    for s in slots:
        s["kind"] = "test"
    when = day.strftime("%a %d %b")
    head = "\U0001f9ea Test run: %s, starts %s-%s, min %d min" % (when, fmt(lo), fmt(hi), MIN_MINUTES)
    if day > bookable_until(now):
        head += "\n(Not bookable yet: that day is released at %s, %d days before.)" % (RELEASE_TIME, ADVANCE_DAYS)
    print("%d matching slots on %s" % (len(slots), day))
    msgs = build_messages(slots)
    if not msgs:
        tg.send(head + "\nNo free slots of that length found.")
    else:
        tg.send(head)
        for text, url in msgs:
            tg.send(text, url)
    if tg.discovered:
        tg.send("Tip: save this number as a repo secret named TELEGRAM_CHAT_ID so alerts always "
                "reach you: <code>%s</code>" % tg.chat_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("watch")
    w.add_argument("--dry-run", action="store_true", help="print instead of sending or saving")
    w.add_argument("--fixture", help="read availability JSON from a file (testing)")
    t = sub.add_parser("test")
    t.add_argument("--venue", default="burgess", help="burgess, tanner or all")
    t.add_argument("--date", default="", help="YYYY-MM-DD (default: 5 days from today)")
    t.add_argument("--start-from", default="07:00")
    t.add_argument("--start-to", default="10:00")
    t.add_argument("--dry-run", action="store_true")
    t.add_argument("--fixture")
    args = parser.parse_args()
    try:
        (cmd_watch if args.cmd == "watch" else cmd_test)(args)
    except RuntimeError as exc:
        print("ERROR:", exc, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
