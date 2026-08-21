# bas-collector

Pulls trend history off a Niagara 4 station over oBIX and writes it into the `bas` PostgreSQL schema.

This is the middle piece:

```
JACE  ──>  bas-collector  ──>  bas-db
```

Read-only, by construction — not by discipline. Requires the `bas-db` migrations to have been applied first.

---

## Setup

You need `bas-db` already running with its migrations applied. Then:

```powershell
cd C:\dev\bas-collector
pip install -r requirements.txt
copy .env.example .env
```

The defaults in `.env.example` point at the built-in mock station and the local database, so you can run everything below without changing anything.

---

## Prove it works before touching a real station

```powershell
python verify_chain.py
```

Starts its own mock Niagara station, runs the full chain into your real Postgres, asserts 29 behaviours, and cleans up after itself. Takes about a minute.

**Expect `29 passed, 0 failed`.** If anything fails, don't point it at a station.

What it actually proves, not just claims:

| | |
|---|---|
| Units and datatypes are captured at ingest | Recovering them later ranges from painful to impossible |
| `$`-escaped names are stored verbatim | Retyping them produces 404s that look like missing points |
| Null *records* are stored as rows | A sensor fault is not the same as no data |
| Re-fetching everything creates zero duplicates | The collector must be safe to re-run, always |
| The roll-horizon guard refuses unsafe collection | See below — this is the one that matters most |
| Overwritten data is recorded as a gap | An unrecorded gap looks identical to equipment being off |
| Checkpoints never advance on failure | This is the entire crash-recovery story |
| A recovered point stops reporting errors | Otherwise monitoring cries wolf permanently |
| A write to the station is refused before a socket opens | What stands between a bug here and someone's chiller |

---

## Try it end to end yourself

Two terminals.

**Terminal 1** — fake station:

```powershell
python mock_station.py
```

**Terminal 2:**

```powershell
python -m collector check
python -m collector discover
python -m collector sync
python -m collector status
```

Then look at what landed:

```sql
SELECT ts_local, point_name, value_num, unit
FROM bas.v_reading ORDER BY ts DESC LIMIT 20;
```

---

## Commands

| | |
|---|---|
| `check` | Config, station reachability, schema. Run this first, always |
| `discover` | Enumerate histories and register them as points. Idempotent |
| `sync` | One collection pass |
| `run` | Sync forever at `POLL_INTERVAL_S` |
| `status` | Collection health, risk summary, recent runs |

Useful flags: `--from-scratch` (ignore checkpoints and re-fetch — safe, it's idempotent), `--only <text>` (limit to matching points), `-v`.

---

## The roll-horizon guard

The most important thing in here, and the reason `discover` ends with a nag.

Niagara's default history capacity is **500 records with overwrite-on-full**. At a 1-minute collection interval that's 8.3 hours before the station destroys data. If the collector polls slower than that, records are lost **permanently, with no error, no log entry, and no gap marker anywhere in Niagara.** You find out months later looking at a chart with holes in it.

So the collector computes `capacity × collection_interval` per point and **refuses to run** if the poll interval doesn't leave a 4× margin. Refuses, not warns — a warning for a silent permanent failure gets scrolled past.

**The catch: oBIX does not expose capacity or collection interval.** They live on the Niagara history *extension*, reachable only through Workbench's History Ext Manager or BQL. Until someone fills them in, the guard reports `roll_horizon_unknown` and says so loudly. **Unknown is never treated as safe.**

```sql
UPDATE bas.point
   SET capacity = 500, collection_interval_s = 900, full_policy = 'roll'
 WHERE niagara_history_name = 'AHU$2d1_SupplyAirTemp';
```

To see what still needs filling in:

```sql
SELECT point_name, roll_risk FROM bas.v_collection_health
WHERE roll_risk = 'roll_horizon_unknown';
```

### Why polling more often is *gentler*

Counterintuitive, worth internalising. Daily data volume is identical either way — frequent polling moves the same data in smaller pieces. Smaller pieces mean lower peak memory per request in the station's heap, shorter-held connections, and a much larger margin before overwrite. The only cost is request count, and an HTTP GET returning 15 rows is nothing to a controller already serving a web UI.

---

## Pointing it at a real station

Edit `.env`:

```
NIAGARA_BASE_URL=https://<jace-ip>
NIAGARA_VERIFY_TLS=0
BAS_SITE_NAME=<the actual building>
```

**Credentials — start with the cookie.** Niagara users default to a digest scheme that HTTP Basic cannot satisfy, so a `401` usually means "this account can't do Basic," not "wrong password" and not "oBIX is broken."

Log into the station's web UI in a browser, press F12 → Application → Cookies, copy the value into `NIAGARA_COOKIE`, and leave user/pass empty. That works immediately.

For production you want a proper service account: Authentication Scheme Name = `HTTPBasicScheme`, **read-only non-superuser role**, Category Service scoped to only the histories needed. Note that such an account cannot log into Workbench — that's expected and correct.

Then:

```powershell
python -m collector check
python -m collector discover
python -m collector sync --only <one point name>
```

**Then verify against Workbench before going further.** Open the same history, same time window, and confirm timestamps and values match exactly. This is the correctness anchor for everything downstream — if it's wrong here it's wrong everywhere, and you won't find out for months.

---

## Design notes

**Read-only is structural.** `obix.py` permits `GET`, and `POST` only to `~historyQuery` / `~historyRollup` — reads that oBIX happens to invoke with POST. Anything else raises before a socket opens.

**POST, not GET, for queries.** Niagara accepts both; Tridium's own training materials ship both. The OASIS oBIX REST binding defines only POST for invoking an operation, and query-string timestamps mangle the `+` in a UTC offset into a space — VOLTTRON's agent bypasses its HTTP client's encoder to work around exactly that. No reason to inherit the problem.

**Checkpoints and readings commit together.** Same transaction, so a checkpoint can never be ahead of committed data. Kill the process at any instant and the next run resumes correctly, because the checkpoint can only ever be behind.

**Backfill and incremental are the same code path.** Different starting points, one loop. Two systems means two sets of bugs and one of them is always the untested one.

**Requests are sequential.** This is a building controller running an occupied building while serving a web UI. 500 points at ~200ms each is under two minutes, comfortably inside a 15-minute cycle. Concurrency would buy time we don't need at a cost the JACE does pay.

**Discovery never overwrites human judgement.** It won't touch `point_role`, `equipment_id`, `capacity`, or `collection_interval_s`. A rediscovery must not undo someone's classification work.

**Points that vanish are deactivated, never deleted.** A history that disappears is usually a rename, and the data it produced is still valid and still ours — the station can no longer supply it.

**`obix.py` is the only Niagara-aware file.** Everything else works with the shapes in `models.py`. A Supervisor is a hostname change; Niagara Data Service, or a different BAS vendor entirely, is a new adapter file.

---

## Running it continuously

```powershell
python -m collector run
```

Syncs every `POLL_INTERVAL_S` until stopped. Survives a station going away — the checkpoint doesn't move, so the next pass catches up.

For a laptop this is fine. **For production it needs to run on a machine the company owns, not a personal one** — that's the entire reason the PHB platform exists, and a collector on someone's laptop with credentials in their head is the exact failure mode it was built to eliminate. Task Scheduler or a service wrapper on an always-on box; the scheduling belongs with the collector, not somewhere else.

---

## Troubleshooting

| Symptom | Almost always |
|---|---|
| `401` | The account can't do HTTP Basic. Not a bad password. Use the cookie to confirm |
| `404` on `/obix/` | Servlet not serving — module not installed, or no Obix Network under Config → Drivers |
| `404` on a history | Name retyped instead of copied. `$20` = space, `$2d` = dash |
| `403` | Authenticated, but no read permission. Check role and Category Service scoping |
| TLS error | Self-signed cert. `NIAGARA_VERIFY_TLS=0` for now; pin the logged fingerprint later |
| Timeout | Firewall dropping packets, or no route from this machine |
| `REFUSED: N points cannot be safely collected` | The guard working correctly. Lower `POLL_INTERVAL_S` or raise capacity on the station |
| `0 records written` every pass | Normal once caught up. Check `status` |
| Everything works, values look wrong | **Stop.** Verify against Workbench before collecting more |

---

## What's next

1. **Point it at the lab station.** `check` first, then `discover`, then one point.
2. **Verify one point against Workbench.** Do not skip this.
3. **Fill in capacity and collection interval** from History Ext Manager, so the guard can actually protect you.
4. **Classify points** — assign `equipment_id` and `point_role`. Unclassified points are invisible to every role-based query, which is most of what makes this data useful.
5. **Then run it continuously** and let real data accumulate.
