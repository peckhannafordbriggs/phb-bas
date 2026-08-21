"""
The Niagara oBIX adapter — the ONLY module that knows oBIX exists.

Everything here is grounded in primary sources, not guessed. The key findings,
recorded in the project's obix-protocol-findings doc:

  * Niagara accepts BOTH a GET with query parameters and a spec-conformant POST
    with an <obj is="obix:HistoryFilter"> body. Tridium's own training materials
    ship both against the same station.

    We POST. The OASIS oBIX REST binding defines only POST for invoking an
    operation; the GET form is a vendor convenience. More practically, query
    strings mangle the "+" in a UTC offset into a space, which is why VOLTTRON's
    agent deliberately bypasses its HTTP client's encoder. No reason to inherit
    that.

  * Units and the value datatype appear ONCE, on a #RecordDef prototype element,
    not on each record. Miss it and units are lost for the whole dataset.

  * Niagara names that prototype #RecordDef; other oBIX servers use #RecordProto.
    Niagara has been observed emitting isNull="true" where the spec says
    null="true". Both spellings are accepted here.

  * Niagara $-hex-escapes special characters in slot names ($20 space, $2d dash).
    Names are used VERBATIM from the parent listing. Decoding is display-only,
    because decode-then-re-encode is not reliably round-trippable and produces
    404s that look exactly like missing points.
"""

from __future__ import annotations

import re
import ssl
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
import urllib3

from .models import (
    HistoryMeta,
    QueryResult,
    RecordDef,
    StationError,
    StationInfo,
    TrendRecord,
)

OBIX_NS = "http://obix.org/ns/schema/1.0"

# Element names in the oBIX object model. Anything else is not part of it.
VALUE_TAGS = {"bool", "int", "real", "str", "enum", "abstime", "reltime", "date", "time", "uri"}
ALL_TAGS = VALUE_TAGS | {"obj", "ref", "op", "list", "err", "feed"}


def _local(tag: str) -> str:
    """Strip the namespace so {http://obix.org/...}obj becomes obj."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def unescape_for_display(name: str) -> str:
    """
    Decode Niagara's $xx slot escapes FOR DISPLAY ONLY.

    Never feed the result back into a URL.
    """
    return re.sub(r"\$([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), name)


def to_obix_time(dt: datetime) -> str:
    """Niagara's wire format: ISO 8601, milliseconds, explicit offset with a colon."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}+00:00"


def parse_obix_time(value: str) -> datetime | None:
    """
    Parse a Niagara timestamp into an aware datetime.

    Handles the trailing Z as well as ±HH:MM, and tolerates a missing
    milliseconds component.
    """
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


class ObixClient:
    """
    Read-only HTTP client for a Niagara station's oBIX servlet.

    Read-only is enforced structurally: the only non-GET this class can issue is
    a POST to a ~historyQuery / ~historyRollup path, which are read operations
    that oBIX happens to invoke with POST. Anything else raises before a socket
    opens. A bug in this collector must not be able to reach building control.
    """

    def __init__(
        self,
        base_url: str,
        username: str | None = None,
        password: str | None = None,
        cookie: str | None = None,
        verify_tls: bool = True,
        timeout_s: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.verify_tls = verify_tls

        self.session = requests.Session()
        self.session.headers["Accept"] = "text/xml, application/xml, */*"
        self.session.headers["User-Agent"] = "phb-bas-collector/0.1 (read-only)"

        if cookie:
            self.session.headers["Cookie"] = cookie
        elif username:
            self.session.auth = (username, password or "")

        if not verify_tls:
            self.session.verify = False
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # -- transport ---------------------------------------------------------

    def certificate_fingerprint(self) -> str | None:
        """
        SHA-256 fingerprint of the station's TLS certificate.

        Niagara ships a self-signed certificate that encrypts but does not verify
        identity. Logging this every run means we can pin it later instead of
        disabling verification permanently — which on a flat building network is
        a man-in-the-middle waiting to happen.
        """
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https":
            return None
        try:
            der = ssl.get_server_certificate(
                (parsed.hostname or "", parsed.port or 443)
            ).encode()
            import hashlib

            pem_body = b"".join(
                line for line in der.splitlines() if not line.startswith(b"-----")
            )
            import base64

            return hashlib.sha256(base64.b64decode(pem_body)).hexdigest()
        except Exception:  # noqa: BLE001 - diagnostic only, never fatal
            return None

    def _request(self, path: str, method: str = "GET", body: str | None = None) -> str:
        url = path if path.startswith("http") else f"{self.base_url}{path}"

        if method != "GET":
            if not (method == "POST" and re.search(r"~history(Query|Rollup)/?$", urlparse(url).path)):
                raise StationError(
                    f"Refusing {method} {url}",
                    "This collector is read-only by construction. The only non-GET it permits "
                    "is POST to ~historyQuery / ~historyRollup, which are reads that oBIX "
                    "invokes with POST. Writing to a station belongs in a separate tool with "
                    "separate credentials and a human confirmation step.",
                )

        try:
            resp = self.session.request(
                method,
                url,
                data=body.encode("utf-8") if body else None,
                headers={"Content-Type": "text/xml; charset=utf-8"} if body else None,
                timeout=self.timeout_s,
            )
        except requests.exceptions.SSLError as exc:
            raise StationError(
                f"TLS verification failed for {url}",
                "Niagara ships a self-signed certificate. Set NIAGARA_VERIFY_TLS=0 for a "
                "diagnostic run; for production, pin the fingerprint or install a real "
                "certificate.",
                exc,
            ) from exc
        except requests.exceptions.ConnectTimeout as exc:
            raise StationError(
                f"Timed out connecting to {url}",
                "A firewall is probably dropping packets silently, or there is no route to "
                "the station from this machine.",
                exc,
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise StationError(
                f"Could not connect to {url}",
                "Nothing is listening on that host and port. Check the station's WebService "
                "property sheet: Https Enabled, Https Port, and Https Only.",
                exc,
            ) from exc

        if resp.status_code == 401:
            raise StationError(
                f"401 Unauthorized for {url}",
                "Almost always the HTTP Basic problem rather than a wrong password. Niagara "
                "users default to a digest scheme that Basic cannot satisfy. The service "
                "account needs Authentication Scheme Name = HTTPBasicScheme, and such an "
                "account cannot log into Workbench or the web UI. To test before one exists, "
                "pass a browser session cookie instead.",
            )
        if resp.status_code == 403:
            raise StationError(
                f"403 Forbidden for {url}",
                "Authenticated, but this account lacks read permission on the history space. "
                "Check its role and Category Service scoping.",
            )
        if resp.status_code == 404:
            raise StationError(
                f"404 Not Found for {url}",
                "If this was /obix/ itself, the servlet is not serving — the module may not be "
                "installed, or no Obix Network exists under Config > Drivers. If it was a "
                "history path, the name is wrong: Niagara $-escapes special characters "
                "($20 space, $2d dash). Copy names verbatim from the parent listing.",
            )
        if not resp.ok:
            raise StationError(
                f"HTTP {resp.status_code} for {url}",
                f"Unexpected status. First 300 characters of body:\n{resp.text[:300]}",
            )

        return resp.text

    # -- parsing helpers ---------------------------------------------------

    @staticmethod
    def _root(xml_text: str) -> ET.Element:
        try:
            return ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise StationError(
                "Response was not parseable XML",
                "If the body looks like HTML you probably hit a login page rather than the "
                "oBIX servlet, which means authentication is not working.",
                exc,
            ) from exc

    @staticmethod
    def _children(node: ET.Element) -> list[tuple[str, ET.Element]]:
        return [(_local(c.tag), c) for c in node if _local(c.tag) in ALL_TAGS]

    @classmethod
    def _by_name(cls, node: ET.Element, name: str) -> ET.Element | None:
        for _tag, child in cls._children(node):
            if child.get("name") == name:
                return child
        return None

    # -- public API --------------------------------------------------------

    def about(self) -> StationInfo:
        """
        Read /obix/about.

        Worth calling first: it reports the exact station name and the Niagara
        version, both of which otherwise require platform credentials.
        """
        root = self._root(self._request("/obix/about/"))

        def val(name: str) -> str | None:
            el = self._by_name(root, name)
            return el.get("val") if el is not None else None

        return StationInfo(
            station_name=val("serverName"),
            product_name=val("productName"),
            product_version=val("productVersion"),
            vendor_name=val("vendorName"),
            timezone=val("tz"),
            server_time=val("serverTime"),
        )

    def list_stations(self) -> list[str]:
        """Station names present in the history space, verbatim."""
        root = self._root(self._request("/obix/histories/"))
        return [
            child.get("name") or ""
            for _tag, child in self._children(root)
            if child.get("name")
        ]

    def list_histories(self, station: str) -> list[str]:
        """History names within a station, verbatim and URL-ready."""
        root = self._root(self._request(f"/obix/histories/{station}/"))
        return [
            child.get("name") or ""
            for _tag, child in self._children(root)
            if child.get("name")
        ]

    def history_meta(self, station: str, name: str) -> HistoryMeta:
        """Read a single history object: count, extent, timezone, and its query op."""
        root = self._root(self._request(f"/obix/histories/{station}/{name}/"))

        def val(child_name: str) -> str | None:
            el = self._by_name(root, child_name)
            return el.get("val") if el is not None else None

        count_raw = val("count")
        query_href = None
        for tag, child in self._children(root):
            if tag == "op" and child.get("name") == "query":
                query_href = child.get("href")
                break

        return HistoryMeta(
            station=station,
            name=name,
            display_name=unescape_for_display(name),
            count=int(count_raw) if count_raw and count_raw.isdigit() else None,
            start=parse_obix_time(val("start") or ""),
            end=parse_obix_time(val("end") or ""),
            timezone=val("tz") or root.get("tz"),
            query_href=query_href,
        )

    def query(
        self,
        station: str,
        name: str,
        start: datetime | None,
        end: datetime | None,
        limit: int,
        query_href: str | None = None,
    ) -> QueryResult:
        """
        Fetch records for one history over a bounded window.

        `limit` is mandatory rather than optional. An unbounded query against a
        JACE is asking a building controller to materialize an arbitrary result
        set in heap while it is also running control logic. Niagara's own preset
        queries cap at 1000, which is the framework telling us the same thing.
        """
        base = f"/obix/histories/{station}/{name}/"
        if query_href and not query_href.startswith("http"):
            path = f"{base}{query_href.lstrip('./')}"
        else:
            path = f"{base}~historyQuery/"
        path = re.sub(r"(?<!:)//+", "/", path)

        parts = [f'  <int name="limit" val="{limit}"/>']
        if start:
            parts.append(f'  <abstime name="start" val="{to_obix_time(start)}"/>')
        if end:
            parts.append(f'  <abstime name="end" val="{to_obix_time(end)}"/>')
        body = '<obj is="obix:HistoryFilter">\n' + "\n".join(parts) + "\n</obj>\n"

        root = self._root(self._request(path, method="POST", body=body))
        return self._parse_query(root)

    @classmethod
    def _parse_query(cls, root: ET.Element) -> QueryResult:
        result = QueryResult()

        # The prototype carries units and datatype for every record in the
        # response. Niagara calls it #RecordDef; other servers use #RecordProto.
        proto = None
        for tag, child in cls._children(root):
            href = child.get("href") or ""
            is_attr = child.get("is") or ""
            if tag == "obj" and (
                href in ("#RecordDef", "#RecordProto") or "obix:HistoryRecord" in is_attr
            ):
                proto = child
                break

        if proto is not None:
            value_proto = cls._by_name(proto, "value")
            if value_proto is not None:
                result.definition.data_type = _local(value_proto.tag)
                result.definition.unit = cls._clean_unit(value_proto.get("unit"))
            ts_proto = cls._by_name(proto, "timestamp")
            if ts_proto is not None:
                result.definition.timezone = ts_proto.get("tz")

        # Prefer the list named "data"; fall back to the first list, because we
        # have not verified that attribute across every Niagara version.
        data_list = None
        for tag, child in cls._children(root):
            if tag == "list" and child.get("name") == "data":
                data_list = child
                break
        if data_list is None:
            for tag, child in cls._children(root):
                if tag == "list":
                    data_list = child
                    break

        if data_list is not None:
            for tag, entry in cls._children(data_list):
                if tag != "obj":
                    continue
                ts_el = cls._by_name(entry, "timestamp")
                if ts_el is None:
                    continue
                ts = parse_obix_time(ts_el.get("val") or "")
                if ts is None:
                    continue

                val_el = cls._by_name(entry, "value")
                record = cls._build_record(ts, val_el, entry, result.definition)
                result.records.append(record)

        count_el = cls._by_name(root, "count")
        if count_el is not None and (count_el.get("val") or "").isdigit():
            result.reported_count = int(count_el.get("val"))

        return result

    @staticmethod
    def _clean_unit(raw: str | None) -> str | None:
        """
        Normalise the unit from the #RecordDef prototype.

        Niagara emits `unit="obix:units/null"` when a point has no unit
        configured in its facets. Naively stripping the prefix stores the
        literal string "null", which is strictly worse than storing nothing:

          * every "does this point have units" check passes
          * the unit_mismatch flag in v_setpoint_pair never fires, so comparing
            a degF measurement against a degC setpoint produces a confident
            wrong answer with no warning
          * an AI asked what units a value is in gets told "null"

        Observed on a real Niagara 4.15 station, not hypothetical.
        """
        if not raw:
            return None
        cleaned = raw.replace("obix:units/", "").strip()
        if not cleaned or cleaned.lower() in ("null", "none", "obix:units/null"):
            return None
        return cleaned

    @staticmethod
    def _build_record(
        ts: datetime, val_el: ET.Element | None, entry: ET.Element, definition: RecordDef
    ) -> TrendRecord:
        status = None
        if val_el is not None:
            status = val_el.get("status")
        status = status or entry.get("status")

        if val_el is None:
            return TrendRecord(ts=ts, status=status)

        # Niagara has been observed using isNull; the spec says null.
        nulled = val_el.get("null") == "true" or val_el.get("isNull") == "true"
        raw = val_el.get("val")
        if nulled or raw is None:
            return TrendRecord(ts=ts, status=status)

        kind = _local(val_el.tag) or definition.data_type
        if kind in ("real", "int"):
            try:
                return TrendRecord(ts=ts, value_num=float(raw), status=status)
            except ValueError:
                return TrendRecord(ts=ts, value_str=raw, status=status)
        if kind == "bool":
            return TrendRecord(ts=ts, value_bool=raw == "true", status=status)
        return TrendRecord(ts=ts, value_str=raw, status=status)

    def probe_definition(self, station: str, name: str, query_href: str | None) -> RecordDef:
        """
        One-record query purely to capture units and datatype.

        Used at discovery time. Units are only ever available in a query
        response, never on the history object itself, and recovering them for
        historical data afterwards ranges from painful to impossible.
        """
        result = self.query(station, name, None, None, limit=1, query_href=query_href)
        return result.definition
