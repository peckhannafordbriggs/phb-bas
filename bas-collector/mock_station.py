#!/usr/bin/env python3
"""
Mock Niagara oBIX station.

Lets the whole chain be proven — fake JACE -> collector -> real Postgres —
without touching a live building controller. When you eventually point the
collector at the lab station, the only untested variable left is whether oBIX is
enabled on it.

The XML here mirrors what real Niagara emits, including three quirks worth
reproducing because they are exactly the things a naive client gets wrong:

  * the record prototype is named #RecordDef, not the spec's #RecordProto
  * null records are marked isNull="true", not the spec's null="true"
  * names are $-hex-escaped ($20 space, $2d dash)

It also enforces authentication, so the 401 path gets exercised.

    python mock_station.py                  serve on 127.0.0.1:8099
    python mock_station.py --port 9000
    python mock_station.py --roll-fast      histories that overwrite quickly,
                                            for testing the roll-horizon guard
"""

from __future__ import annotations

import argparse
import math
import re
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

STATION = "LabStation"
TZ = "America/New_York"

HISTORIES = [
    # name (as Niagara escapes it), unit, oBIX type, capacity, interval seconds
    ("AHU$2d1_SupplyAirTemp",    "fahrenheit",     "real", 500, 900),
    ("AHU$2d1_SupplyAirTempSp",  "fahrenheit",     "real", 500, 900),
    ("AHU$2d1_SupplyFanCmd",     None,             "bool", 500, 900),
    ("AHU$2d1_SupplyFanStatus",  None,             "bool", 500, 900),
    ("AHU$2d1_CoolingValve",     "percent",        "real", 500, 900),
    ("AHU$2d1_HeatingValve",     "percent",        "real", 500, 900),
    ("OutsideAirTemp",           "fahrenheit",     "real", 500, 900),
    ("Boiler$20Room_Pressure",   "inches_of_water", "real", 500, 60),
]

ROLL_FAST = False


def obix_time(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}+00:00"


def xml(body: str) -> bytes:
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{body}'.encode()


def find(name: str):
    for h in HISTORIES:
        if h[0] == name:
            return h
    return None


def capacity_of(h) -> int:
    return 20 if ROLL_FAST else h[3]


def lobby() -> bytes:
    return xml(f"""<obj href="/obix/" xmlns="http://obix.org/ns/schema/1.0">
  <ref name="about" href="about/" is="obix:About"/>
  <op name="batch" href="batch/" in="obix:BatchIn" out="obix:BatchOut"/>
  <ref name="watchService" href="watchService/" is="obix:WatchService"/>
  <ref name="histories" href="histories/"/>
  <ref name="config" href="config/"/>
  <ref name="alarms" href="alarms/"/>
</obj>""")


def about() -> bytes:
    now = datetime.now(timezone.utc)
    boot = now - timedelta(days=41)
    return xml(f"""<obj href="/obix/about/" is="obix:About" xmlns="http://obix.org/ns/schema/1.0">
  <str name="obixVersion" val="1.1"/>
  <str name="serverName" val="{STATION}"/>
  <abstime name="serverTime" val="{obix_time(now)}" tz="{TZ}"/>
  <abstime name="serverBootTime" val="{obix_time(boot)}" tz="{TZ}"/>
  <str name="vendorName" val="Tridium"/>
  <uri name="vendorUrl" val="http://www.tridium.com"/>
  <str name="productName" val="Niagara Station"/>
  <str name="productVersion" val="4.13.2.18"/>
  <str name="tz" val="{TZ}"/>
</obj>""")


def histories_root() -> bytes:
    return xml(f"""<obj href="/obix/histories/" xmlns="http://obix.org/ns/schema/1.0">
  <ref name="{STATION}" href="{STATION}/"/>
</obj>""")


def station_histories() -> bytes:
    refs = "\n".join(
        f'  <ref name="{h[0]}" href="{h[0]}/" is="obix:History"/>' for h in HISTORIES
    )
    return xml(f"""<obj href="/obix/histories/{STATION}/" xmlns="http://obix.org/ns/schema/1.0">
{refs}
</obj>""")


def history_object(h) -> bytes:
    name, _unit, _kind, _cap, interval = h
    cap = capacity_of(h)
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=(cap - 1) * interval)
    return xml(f"""<obj href="/obix/histories/{STATION}/{name}/" is="obix:History" xmlns="http://obix.org/ns/schema/1.0">
  <int name="count" val="{cap}"/>
  <abstime name="start" val="{obix_time(start)}" tz="{TZ}"/>
  <abstime name="end" val="{obix_time(end)}" tz="{TZ}"/>
  <str name="tz" val="{TZ}"/>
  <op name="query" href="~historyQuery/" in="obix:HistoryFilter" out="obix:HistoryQueryOut"/>
  <op name="rollup" href="~historyRollup/" in="obix:HistoryRollupIn" out="obix:HistoryRollupOut"/>
</obj>""")


def snap(dt: datetime, interval: int) -> datetime:
    """
    Align a timestamp to the collection grid.

    This matters more than it looks. A real historian logs on a fixed schedule,
    so querying the same window twice returns the SAME timestamps — which is
    exactly what makes the collector's idempotency meaningful. An earlier version
    of this mock generated timestamps relative to the moment of the request, so
    every query produced fresh ones and re-running appeared to write duplicates
    when in fact it was writing genuinely new records. Snapping to a grid
    anchored at the Unix epoch reproduces real behaviour.
    """
    return datetime.fromtimestamp(
        (int(dt.timestamp()) // interval) * interval, tz=timezone.utc
    )


def value_for(h, ts: datetime) -> str:
    """
    Value is a pure function of the timestamp, never of the request time.

    Same reason as snap(): querying the same instant twice must give the same
    number, or nothing downstream can be verified.
    """
    _name, unit, kind, _cap, interval = h
    index = int(ts.timestamp()) // interval
    if kind == "bool":
        return "true" if index % 96 < 60 else "false"
    base = 45.0 if unit == "percent" else (1.2 if unit == "inches_of_water" else 55.0)
    swing = 25.0 if unit == "percent" else (0.3 if unit == "inches_of_water" else 6.0)
    return f"{base + swing * math.sin(index / 96 * 2 * math.pi):.2f}"


def history_query(h, body: str) -> bytes:
    name, unit, kind, _cap, interval = h
    cap = capacity_of(h)

    def grab(field: str):
        m = re.search(rf'name="{field}"\s+val="([^"]+)"', body)
        return m.group(1) if m else None

    limit = int(grab("limit") or 1000)
    now = datetime.now(timezone.utc)

    def parse(v):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

    end = min(parse(grab("end")) or now, now)
    start = parse(grab("start")) or (end - timedelta(days=1))

    # The station only holds `cap` records. Anything older has been overwritten
    # and is gone — this is what makes the roll-overwrite path testable.
    earliest = snap(now, interval) - timedelta(seconds=(cap - 1) * interval)
    if start < earliest:
        start = earliest

    ts = snap(start, interval)
    if ts < start:
        ts += timedelta(seconds=interval)

    rows = []
    while ts <= end and len(rows) < limit:
        # Deterministically null every 137th slot, so the collector's null
        # handling is exercised without the choice depending on request timing.
        slot = int(ts.timestamp()) // interval
        if kind == "real" and slot % 137 == 0:
            rows.append(
                f'    <obj>\n      <abstime name="timestamp" val="{obix_time(ts)}" tz="{TZ}"/>\n'
                f'      <{kind} name="value" isNull="true" status="{{down}}"/>\n    </obj>'
            )
        else:
            rows.append(
                f'    <obj>\n      <abstime name="timestamp" val="{obix_time(ts)}" tz="{TZ}"/>\n'
                f'      <{kind} name="value" val="{value_for(h, ts)}"/>\n    </obj>'
            )
        ts += timedelta(seconds=interval)

    unit_attr = f' unit="obix:units/{unit}"' if unit else ""
    last = start + timedelta(seconds=max(0, len(rows) - 1) * interval)

    return xml(f"""<obj href="/obix/histories/{STATION}/{name}/~historyQuery/" is="obix:HistoryQueryOut" xmlns="http://obix.org/ns/schema/1.0">
  <int name="count" val="{len(rows)}"/>
  <abstime name="start" val="{obix_time(start)}" tz="{TZ}"/>
  <abstime name="end" val="{obix_time(last)}" tz="{TZ}"/>
  <obj href="#RecordDef" is="obix:HistoryRecord">
    <abstime name="timestamp" isNull="true" tz="{TZ}"/>
    <{kind} name="value" isNull="true"{unit_attr}/>
  </obj>
  <list name="data" of="#RecordDef obix:HistoryRecord">
{chr(10).join(rows)}
  </list>
</obj>""")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # quiet; the collector's own logging is what matters

    def _send(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") or self.headers.get("Cookie"):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Niagara"')
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.end_headers()
        self.wfile.write(xml('<err is="obix:BadUriErr" display="Unauthorized"/>'))
        return False

    def do_GET(self):  # noqa: N802
        if not self._authorized():
            return
        path = self.path.rstrip("/") or "/"
        if path == "/obix":
            return self._send(200, lobby())
        if path == "/obix/about":
            return self._send(200, about())
        if path == "/obix/histories":
            return self._send(200, histories_root())
        if path == f"/obix/histories/{STATION}":
            return self._send(200, station_histories())
        m = re.match(rf"^/obix/histories/{STATION}/(.+)$", path)
        if m and find(m.group(1)):
            return self._send(200, history_object(find(m.group(1))))
        self._send(404, xml('<err is="obix:BadUriErr" display="Not found"/>'))

    def do_POST(self):  # noqa: N802
        if not self._authorized():
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        path = self.path.rstrip("/") or "/"
        m = re.match(rf"^/obix/histories/{STATION}/(.+)/~historyQuery$", path)
        if m:
            h = find(m.group(1))
            if h:
                return self._send(200, history_query(h, body))
        self._send(404, xml('<err is="obix:BadUriErr" display="Not found"/>'))


def main() -> None:
    global ROLL_FAST
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--roll-fast", action="store_true",
                    help="tiny history capacity, for testing the roll-horizon guard")
    args = ap.parse_args()
    ROLL_FAST = args.roll_fast

    print(f"\nMock Niagara oBIX station on http://127.0.0.1:{args.port}")
    print(f"Station: {STATION}   histories: {len(HISTORIES)}")
    if ROLL_FAST:
        print("ROLL-FAST mode: capacity 20 records — histories overwrite almost immediately")
    print("\nCtrl+C to stop.\n")
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
