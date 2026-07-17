# pg-load-kit — Synthetic Production-Write-Traffic Generator

Reproduces a production **write** pattern against a Postgres **fork/clone** (or a
UAT/DEV database) so a plan-comparison or capacity trial is a fair read. Built for
the three hot write paths teams most often need to reproduce:

1. **SKIP LOCKED job-queue churn** — `FOR UPDATE SKIP LOCKED` claim + enqueue.
2. **Integration-sync UPDATE volume** — skewed (zipfian) UPDATEs on a large table.
3. **Materialized-view refresh** — run on an interval, alongside the write load.

It is **data-driven**: you set the traffic mix from your real `pg_stat_statements`
call ratios, not by guessing.

---

## How it works

Before you move a database to a new plan, region, or bigger server, you want to know:
*"will it hold up under our real write traffic?"* You can't hammer production, and a
fork copies the **data** but not the **traffic** — a fork sits idle. This kit
**generates realistic write traffic** against a safe copy so you can watch how it
behaves under load before you commit to the change.

### Why it works on *any* database

The tool **doesn't know anything about your tables in advance.** When you connect it to
a database, the first thing it does is *look inside* — it reads the tables, columns,
data types, primary keys, foreign-key relationships, and enum (fixed-choice) fields.
Then it **writes its test scripts from what it just discovered.** Because every test is
built fresh from whatever database you point it at, nothing is hardcoded. Point it at an
orders DB, a customer DB, a Titanic passenger dataset — it inspects each and adapts.
Change the connection string, click **Discover**, and it re-learns the new schema.

In particular, the "producer" INSERT is synthesized from live metadata: it skips
auto-generated/defaulted columns, resolves foreign-key columns to a random *existing*
referenced row (so constraints hold), picks a valid label for enum columns, and
generates a type-appropriate value for every other required column. No hand-editing per
table.

### The three write patterns it reproduces

1. **Queue churn** — workers claiming "to-do" rows and marking them done (`SKIP LOCKED`).
2. **Update volume** — a steady stream of UPDATEs to a large table (a sync process).
3. **Report refresh** — periodically rebuilding a materialized view under that load.

You control the **mix** (the weights), so it matches *your* real traffic.

### Step by step (browser control panel)

1. **Connect** — paste any `DATABASE_URL`. Read-only inspection is safe against prod; for
   an actual load test point it at a **fork / dev / UAT** copy, never production.
2. **🔍 Discover** — the tool inspects the DB and pre-fills a suggested mapping (queue
   table, big update-target table, key columns). These are *guesses* — you confirm them.
3. **Confirm the mapping** — via dropdowns, pick which table is the queue and its status
   values (e.g. `pending` → `shipped`), the big update-target table, and optionally a
   matview. You choose from the columns it actually found — nothing typed by hand.
4. **✓ Validate mapping** — runs each statement inside a transaction that is *immediately
   rolled back* (nothing is written), purely to confirm the SQL matches your schema.
   Green ✅ per script, or a clear ❌ with the exact reason. Fix any red before running.
5. **Set the load** — clients, duration, warmup, and the traffic mix; tick the
   **FORK/CLONE** confirmation.
6. **Generate & run load** — warms up, then runs the measured test.
7. **Read the results** — throughput (tps), latency, transactions, auto-interpreted
   notes (e.g. a network-bound warning), plus DB-side health (writes, cache hit, top
   queries, locks, size). **📊 Refresh metrics** gives a read-only snapshot anytime.

### Two honest caveats

- **Run it close to the database.** pgbench measures from wherever it runs, so a laptop →
  distant-cloud test is dominated by network travel, not the DB — numbers look
  artificially slow. For a real capacity number, run it in the DB's **region** (Heroku
  one-off dyno / same-region EC2). The UI flags this automatically.
- **It simulates, it doesn't replay.** It reproduces the *shape* of your write traffic in
  your chosen mix; it does not replay exact historical queries (see below for why).

---

## Why this exists — "replay" vs. "simulate"

A common customer ask:

> *"For the trial to be fair we need to reproduce our production write pattern against
> the fork: SKIP LOCKED job-queue churn, integration-sync UPDATE volume, and the
> materialized-view refresh. If Heroku has tooling for replaying or simulating
> production write traffic we'd welcome guidance; otherwise we'll build a synthetic
> generator from our hottest write paths."*

**The honest answer:**

- **Replay** (capture real production statements and re-run them verbatim): Heroku has
  **no** first-party tool. Postgres doesn't record replayable statements by default,
  and `pg_stat_statements` stores *normalized* queries (literals stripped), so you
  cannot replay from it. True replay needs full statement logging + an external tool
  (e.g. `pgreplay`) — heavy, and it captures PII.
- **Simulate** (model the shape of the hottest write paths and generate synthetic
  traffic matching it): also no first-party tool — but this is the **recommended
  approach**, and it's what this kit does.
- **Forks** (`heroku pg:copy` / fork) copy the **data**, not the **traffic**. They give
  you a safe substrate to load against; they don't generate load.

So: build a synthetic generator, shaped by your real `pg_stat_statements` ratios, and
run it against a fork or a disposable UAT/DEV DB. **That is this kit.**

---

## Requirements

- **`psql` and `pgbench`** — the Postgres client tools (`pgbench` ships with them).
  - macOS: `brew install libpq && brew link --force libpq`
- **`python3`** — stdlib only, no packages to install (for the browser control panel).
- **`pg_stat_statements`** enabled on production (default on Heroku Postgres) — used to
  derive the real traffic mix.

Check you have the tools:
```bash
psql --version && pgbench --version
```

---

## Quick start (browser control panel — recommended)

```bash
git clone https://github.com/chetankd10/pg-load-kit.git
cd pg-load-kit
python3 server.py            # then open http://127.0.0.1:8765
```

You'll see:
```
pg-load-kit control panel → http://127.0.0.1:8765
psql: found | pgbench: found
```

Open **http://127.0.0.1:8765** and follow the 7 steps below.

### The UI workflow, step by step

1. **Connection** — paste a `DATABASE_URL`.
   - Heroku: `heroku config:get DATABASE_URL -a <app>`. Add `?sslmode=require` if it
     errors on SSL.
2. **🔍 Discover schema** — introspects tables/columns/PKs/matviews and auto-suggests a
   queue table and a big UPDATE-target table. *(The suggestion is only a guess — always
   confirm it.)*
3. **Schema mapping** — confirm/override the tables + columns via dropdowns:
   - **Job-queue table:** the table your SKIP LOCKED workers claim from. Set the status
     column and the actual **"queued"/"running" values** for it (e.g. `pending`/`shipped`).
   - **Big UPDATE-target table:** the large table your sync process updates. Set PK,
     updated-at column, payload (`(none)` if there isn't one), and **Max PK** = the real
     `max(pk)` so zipfian IDs hit existing rows.
   - **Matview:** optional; needs a UNIQUE index for `CONCURRENTLY` refresh.
4. **✓ Validate mapping** — runs each generated statement inside a **rolled-back**
   transaction (nothing is written) and reports per-script pass/fail, so you catch a
   wrong column/enum/FK *before* loading. Fix any ❌ before running.
5. Set **Run settings** (clients, threads, duration, warmup, weights), tick the
   **FORK/CLONE** confirmation, then **Generate & run load**.
6. **Results** — stat cards (tps, latency, txns), auto-notes (e.g. "network-bound"),
   per-script breakdown, and DB-side metrics (write activity, cache hit, top queries,
   locks, size).
7. **📊 Refresh metrics** — pull DB health read-only anytime.

**Safety model in the UI:** read-only actions (**Discover**, **Check data**, **Refresh
metrics**) are safe against production. Write actions (**Run load**) require the
FORK/CLONE confirmation.

### Weights (W consumer / W producer / W update)

These are the **relative ratios** pgbench uses to pick which script runs next:
- `6 / 2 / 5` → ~46% queue claims, ~15% enqueue, ~38% sync UPDATEs.
- Set a weight to **0** to skip that pattern entirely (e.g. `W producer=0` if your
  target table's INSERT can't be synthesized because of NOT-NULL/FK columns).
- Derive real values from production (see below), don't guess.

---

## Command-line usage (no UI)

Edit the three template scripts in `scripts/` to your schema, then run the driver:

```bash
I_UNDERSTAND_THIS_IS_NOT_PROD=yes \
TARGET_DATABASE_URL="postgres://…fork…?sslmode=require" \
W_CONSUMER=6 W_PRODUCER=2 W_UPDATE=5 \
CLIENTS=60 THREADS=12 DURATION=900 WARMUP=120 \
./run_load.sh
```

The driver: ANALYZEs, resets stats, runs a discarded **warmup**, then the **measured**
run, and prints the pgbench summary. Inspect afterwards with Heroku pg-extras
(`pg:outliers`, `pg:locks`, `pg:vacuum-stats`, `pg:cache-hit`).

### Deriving the real write mix (do this once, read-only, on prod)
```bash
psql "$PROD_DATABASE_URL" -f scripts/derive_weights.sql
```
Turn the `pct_calls` ratios into small integer weights (e.g. 60% → `W_CONSUMER=6`).

### Smoke test with a throwaway schema
No real tables handy? Seed disposable `jobs`/`big_table` tables:
```bash
psql "$TARGET_DATABASE_URL" -f scripts/demo_schema.sql
```

---

## Tunables (env vars for the CLI; same fields exist in the UI)

| Var | Default | Meaning |
|-----|---------|---------|
| `TARGET_DATABASE_URL` | — | Fork/clone/UAT connection string (**required**) |
| `CLIENTS` | 60 | Concurrent pgbench clients (drives contention) |
| `THREADS` | 12 | pgbench worker threads |
| `DURATION` | 900 | Measured run length, seconds |
| `WARMUP` | 120 | Warmup seconds (discarded before measuring) |
| `W_CONSUMER`/`W_PRODUCER`/`W_UPDATE` | 6/2/5 | Script weights (from derive_weights.sql) |
| `RUN_MATVIEW` | 1 | Also run the refresh loop |
| `MVIEW` | my_mv | Materialized view name |
| `REFRESH_INTERVAL` | 60 | Seconds between refreshes |
| `REFRESH_CONCURRENTLY` | CONCURRENTLY | Set empty for a plain (locking) refresh |
| `CONSUMER_SQL`/`PRODUCER_SQL`/`UPDATE_SQL` | scripts/*.sql | Override script paths (the UI uses this) |

---

## ⚠️ Run *in-region* for a real capacity number

pgbench measures throughput from the **client's** side, so it includes network
round-trip time. Running from a laptop to a cloud DB (e.g. Mac → AWS us-east-1) caps
throughput at ~1/RTT — you'll see low tps (tens) and high latency (hundreds of ms)
**regardless of how powerful the database is**. That is a client-location artifact, not
a DB limit; the UI flags it automatically, and you can confirm it by comparing pgbench's
client-side latency against the `mean_ms` in the "Top write queries" panel (server-side).

For a meaningful result, run the generator **close to the DB**:
- **Heroku one-off dyno:** `heroku run bash -a <app>`, then run the kit there.
- **Same-region EC2** in the DB's region.

From your laptop, the tool is still perfect for **validating the SQL** and confirming the
patterns work — just not for the final tps number.

---

## Layout
```
pg-load-kit/
├── server.py                      # browser control panel (Discover→Validate→Run→Metrics)
├── run_load.sh                    # CLI driver — reset stats, warmup, measured run
├── scripts/
│   ├── derive_weights.sql         # READ-ONLY: run on PROD once to get the traffic mix
│   ├── consumer_skiplocked.sql    # queue consumer (SKIP LOCKED)   [template + CLI default]
│   ├── producer_enqueue.sql       # queue producer (INSERT)        [template + CLI default]
│   ├── integration_update.sql     # large-table UPDATE (zipfian)   [template + CLI default]
│   ├── refresh_matview.sh         # matview refresh loop           [template + CLI default]
│   ├── demo_schema.sql            # optional throwaway jobs/big_table for a smoke test
│   └── generated/                 # runtime scripts built by the UI (gitignored)
└── README.md
```

The three `.sql` templates document the write patterns and are the CLI defaults. The
UI does **not** edit them — it writes fresh scripts into `scripts/generated/` from your
Discover→mapping, so the kit never gets coupled to one schema.

---

## Fairness gotchas (handled, but know them)

- **Warmup discarded** — a fresh fork starts cold with zeroed stats; the driver warms
  up, resets stats, then measures.
- **Run long enough for autovacuum** — SKIP LOCKED + high UPDATE volume create heavy
  dead tuples; `DURATION=900`+ lets autovacuum/bloat show up.
- **Writes only hit the leader** — on Heroku Postgres Advanced, follower pools do NOT
  offload writes. Size the **leader** for the trial.
- **Match `fillfactor`/HOT behavior** on your tables to prod, or UPDATE cost skews.
- **`-n` is required** — the kit runs custom (non-`pgbench_*`) tables, so pgbench's
  pre-run vacuum is skipped (per the pgbench docs). Already set in `run_load.sh`.

---

## Safety

- `run_load.sh` refuses to run unless `I_UNDERSTAND_THIS_IS_NOT_PROD=yes`.
- `server.py` binds to **127.0.0.1 only** (not exposed off the machine).
- **Run load** requires the FORK/CLONE confirmation; **Discover / Check / Refresh
  metrics** are read-only and safe against prod.
- Always confirm no app/integration is wired to the target DB before loading it.

---

## Troubleshooting

| Symptom | Cause & fix |
|---------|-------------|
| `number of transactions actually processed: 0` | Every statement errored. Click **✓ Validate mapping** — it names the failing script + Postgres error. Usually a wrong status value/enum, a wrong table, or duplicate column roles. |
| `invalid input value for enum …` | The status value (e.g. `queued`) isn't valid for your enum. Set the real values (e.g. `pending`/`shipped`). |
| `column "x" specified more than once` | Two mapping dropdowns point at the same column. Give each role a distinct column, or set unused ones to `(none)`. |
| `column "id" is of type integer but expression is of type text` | The producer INSERT can't be synthesized for this table (typed/FK columns). Set **W producer = 0**. |
| SSL / connection errors on Heroku | Append `?sslmode=require` to the URL. |
| Very low tps (tens), high latency (hundreds of ms) | Network-bound — you're running far from the DB. Run in-region (see above). |
