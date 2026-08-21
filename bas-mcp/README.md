# bas-mcp

Lets Claude answer questions about your building data.

It's an MCP server — a small program Claude Desktop runs in the background that gives Claude a set of tools for querying the BAS database. You then just ask questions in plain English.

```
You:    "Is anything wrong with the building?"
Claude: [runs the fault rules] "AHU-2's supply fan is commanded on but
         reporting off for 48 intervals on the 16th and 17th..."
```

No API key. No extra cost. Uses the Claude you already have.

---

## Setup

### 1. Install

```powershell
cd C:\dev\bas-mcp
pip install -r requirements.txt
```

### 2. Create the read-only database user

```powershell
psql "postgresql://bas:bas_local_dev_only@localhost:5432/bas" -f setup_readonly_role.sql
```

This is the important step. It creates a database account that **physically cannot modify anything**. See *Why read-only matters* below.

### 3. Test it before wiring up Claude

```powershell
$env:BAS_READONLY_URL="postgresql://bas_readonly:bas_readonly_local@localhost:5432/bas"
python test_tools.py
```

**Expect `21 passed, 0 failed`.** Among other things this proves that DELETE, DROP, UPDATE and stacked statements are all refused — both by the query validator and by the database role itself.

### 4. Point Claude Desktop at it

Open the config file (create it if it doesn't exist):

```powershell
notepad "$env:APPDATA\Claude\claude_desktop_config.json"
```

Paste this in. If the file already has content, merge the `bas` entry into the existing `mcpServers` block rather than replacing everything:

```json
{
  "mcpServers": {
    "bas": {
      "command": "python",
      "args": ["C:\\dev\\bas-mcp\\server.py"],
      "env": {
        "BAS_READONLY_URL": "postgresql://bas_readonly:bas_readonly_local@localhost:5432/bas"
      }
    }
  }
}
```

Note the **doubled backslashes** — JSON requires them.

### 5. Restart Claude Desktop

Fully quit it — right-click the system tray icon and Quit, not just close the window. Then reopen.

You should see a tools icon in the chat box. Click it and `bas` should be listed.

---

## Try it

Things worth asking:

- *What points do we have?*
- *Is data still coming in?*
- *What did the room temperature do yesterday?*
- *What's the average room temp over the last week?*
- *Is anything wrong with the building?*
- *Which points aren't classified yet?*

Claude picks the right tool, the database does the computation, and Claude explains the result.

---

## The tools

| Tool | What it does |
|---|---|
| `list_points` | Inventory — what exists, what it measures, where |
| `list_roles` | The point_role vocabulary actually in use |
| `describe_schema` | The annotated schema, so Claude writes correct SQL |
| `get_readings` | Trend data over a window, **auto-aggregated** |
| `summarize_point` | Statistics computed in SQL |
| `find_faults` | Deterministic HVAC fault rules |
| `collection_health` | Is data arriving, is any at risk |
| `run_sql` | Guarded read-only escape hatch |

---

## Two design decisions worth understanding

### Claude never does arithmetic on trend rows

This is the rule the whole thing is built around: **Claude decides what to compute, the tools compute it, Claude explains the result.**

Hand an LLM 50,000 temperature readings and ask for an average, and you'll get a number that looks right and is wrong — with nothing to indicate anything went astray. So `get_readings` refuses to return thousands of rows even if asked. It buckets by time, returns min/avg/max per bucket, and says explicitly that it did so and why.

For exact figures, `summarize_point` computes in SQL. `run_sql` truncates at 500 rows and warns loudly, because a conclusion drawn from a silently truncated result set is wrong in a way nobody notices.

### Why read-only matters more here than usual

Three independent layers block writes: a database role with no write permission, read-only transactions, and a validator that rejects anything that isn't a SELECT.

That's more paranoia than most systems warrant, and the reason is specific: **building history is irreplaceable.** The JACE overwrites its own copy roughly every 42 hours. Once data is in this database, that's the only copy in existence. A bad DELETE can't be restored from the source, because the source no longer has it.

`test_tools.py` verifies all three layers, including that the role itself refuses a write even if the validator were bypassed.

---

## Fault detection

`find_faults` runs deterministic rules, not machine learning. That's deliberate — the classic expensive building faults are all deterministic and explainable, and rules that state their reasoning are far more useful than a model that outputs an anomaly score.

| Rule | Why it costs money |
|---|---|
| Never reaching setpoint | Calling for conditioning it can't deliver — running at full effort and failing |
| Commanded on, not running | Control system believes it's running and it isn't; everything downstream is controlled on a false premise |
| Simultaneous heating and cooling | You pay to heat the air and pay again to cool it back down |
| Stuck sensor | A perfectly flat measurement is a dead sensor. Everything controlling off it is now wrong |
| Running while unoccupied | Conditioning an empty building |

**The stuck-sensor rule excludes setpoints and commands.** A setpoint holding at 55 °F is a setpoint working correctly; a heating valve closed through August is correct behaviour. Flagging those is how a fault system teaches people to ignore it. Only measurements are checked — those are sensing a physical world that always carries some noise.

**Most rules need classified points.** Anything comparing a measurement to its setpoint requires both points to have a `point_role` and be assigned to the same equipment. Unclassified points are invisible to them — which is why `find_faults` caveats a clean result when classification is incomplete. A clean bill of health from unclassified data is weaker evidence than it looks.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `bas` doesn't appear in Claude's tool list | Config file JSON is malformed — check the doubled backslashes. Fully quit and reopen Claude Desktop |
| "No database URL" | `BAS_READONLY_URL` missing from the `env` block in the config |
| Connection refused | Postgres isn't running. `Get-Service postgresql*` |
| Tools appear but every call errors | Run `python test_tools.py` directly — the error will be much clearer than what Claude surfaces |
| `python` not found by Claude Desktop | Use the full path to python.exe in `command`. Find it with `(Get-Command python).Source` |
| Everything works but answers seem thin | Probably unclassified points. `list_points` will say how many |

---

## What would make this better

**Classify the points.** `point_role` and `equipment_id` are what turn "show me this named point" into "compare supply air temperature across every AHU." Most of the interesting questions need them.

**More data.** Fault rules need enough history to distinguish a pattern from a blip. A week is thin; a month is useful.

**Real building data.** Synthetic points from a History Emulator will exercise the machinery, but they won't tell you anything about a building.
