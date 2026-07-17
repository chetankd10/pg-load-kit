#!/usr/bin/env python3
"""
pg-load-kit control panel — a tiny LOCAL web UI to inspect a DB and run the load.

A browser cannot connect to Postgres directly, so this server shells out to the
`psql` / `pgbench` client tools (which you already have) against a DATABASE_URL you
provide in the UI. Binds to 127.0.0.1 only.

Flow:
  1. Paste DATABASE_URL → "Discover schema" introspects tables/columns/matviews.
  2. The UI auto-suggests a job-queue table and a big UPDATE-target table; you
     confirm or override the table + column mapping via dropdowns.
  3. "Generate & run" writes pgbench scripts from YOUR real names, then runs them.

Run:   python3 server.py           # then open http://127.0.0.1:8765
"""
import json, os, re, subprocess, html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(HERE, "scripts", "generated")   # generated pgbench scripts live here
HOST, PORT = "127.0.0.1", 8765

# ---- Read-only inspection queries (safe to run against prod) ----
CHECKS = {
    "overview": """
        SELECT current_database() AS db, version() AS pg_version,
               pg_size_pretty(pg_database_size(current_database())) AS size,
               (SELECT count(*) FROM pg_stat_activity) AS connections;""",
    "write_mix": """
        SELECT calls, rows,
               round(100.0*calls/NULLIF(sum(calls) OVER (),0),1) AS pct_calls,
               left(regexp_replace(query,'\\s+',' ','g'),80) AS query
        FROM pg_stat_statements
        WHERE query ILIKE 'UPDATE%' OR query ILIKE 'INSERT%'
           OR query ILIKE '%SKIP LOCKED%' OR query ILIKE '%REFRESH MATERIALIZED%'
        ORDER BY calls DESC LIMIT 20;""",
    "table_sizes": """
        SELECT relname AS table,
               pg_size_pretty(pg_total_relation_size(relid)) AS total,
               n_live_tup AS live_rows, n_dead_tup AS dead_rows
        FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 20;""",
    "activity": """
        SELECT pid, state, wait_event_type,
               left(regexp_replace(query,'\\s+',' ','g'),70) AS query,
               round(extract(epoch FROM (now()-query_start)))||'s' AS age
        FROM pg_stat_activity WHERE state <> 'idle' AND pid <> pg_backend_pid()
        ORDER BY query_start LIMIT 25;""",
    "locks": """
        SELECT bl.pid AS blocked, ka.pid AS blocking,
               left(regexp_replace(ka.query,'\\s+',' ','g'),60) AS blocking_query
        FROM pg_locks bl
        JOIN pg_stat_activity a ON a.pid = bl.pid
        JOIN pg_locks kl ON kl.locktype=bl.locktype AND kl.pid<>bl.pid AND NOT kl.granted=bl.granted
        JOIN pg_stat_activity ka ON ka.pid = kl.pid
        WHERE NOT bl.granted LIMIT 25;""",
}

# ---- Introspection: return tables (with size, columns, PK) + matviews as JSON ----
# Emitted as one JSON blob by Postgres so the server just forwards it to the UI.
INTROSPECT_SQL = r"""
WITH cols AS (
  SELECT c.table_schema, c.table_name,
         json_agg(json_build_object(
             'name', c.column_name, 'type', c.data_type, 'udt', c.udt_name,
             'nullable', (c.is_nullable = 'YES'),
             'has_default', (c.column_default IS NOT NULL))
           ORDER BY c.ordinal_position) AS columns
  FROM information_schema.columns c
  WHERE c.table_schema NOT IN ('pg_catalog','information_schema')
  GROUP BY c.table_schema, c.table_name
),
pk AS (
  SELECT tc.table_schema, tc.table_name,
         json_agg(kcu.column_name) AS pk_cols
  FROM information_schema.table_constraints tc
  JOIN information_schema.key_column_usage kcu
    ON kcu.constraint_name = tc.constraint_name
   AND kcu.table_schema   = tc.table_schema
  WHERE tc.constraint_type = 'PRIMARY KEY'
  GROUP BY tc.table_schema, tc.table_name
),
fk AS (
  -- foreign keys, one row per (table, fk column) so the producer can resolve a
  -- valid referenced id at INSERT time. Multi-column FKs are matched by position.
  SELECT ns.nspname AS table_schema, cl.relname AS table_name,
         json_agg(json_build_object(
             'column', att.attname,
             'ref_schema', fns.nspname, 'ref_table', fcl.relname,
             'ref_column', fatt.attname)) AS fks
  FROM pg_constraint con
  JOIN pg_class cl      ON cl.oid = con.conrelid
  JOIN pg_namespace ns  ON ns.oid = cl.relnamespace
  JOIN pg_class fcl     ON fcl.oid = con.confrelid
  JOIN pg_namespace fns ON fns.oid = fcl.relnamespace
  JOIN LATERAL unnest(con.conkey)  WITH ORDINALITY AS ck(attnum, ord)  ON true
  JOIN LATERAL unnest(con.confkey) WITH ORDINALITY AS fk2(attnum, ord) ON fk2.ord = ck.ord
  JOIN pg_attribute att  ON att.attrelid = con.conrelid  AND att.attnum = ck.attnum
  JOIN pg_attribute fatt ON fatt.attrelid = con.confrelid AND fatt.attnum = fk2.attnum
  WHERE con.contype = 'f'
  GROUP BY ns.nspname, cl.relname
),
enums AS (
  SELECT json_object_agg(typname, labels) AS e FROM (
    SELECT t.typname, json_agg(e.enumlabel ORDER BY e.enumsortorder) AS labels
    FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
    GROUP BY t.typname
  ) z
),
tabs AS (
  SELECT n.nspname AS schema, c.relname AS name,
         pg_total_relation_size(c.oid) AS bytes,
         COALESCE(s.n_live_tup, 0) AS live_rows
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
  WHERE c.relkind = 'r'
    AND n.nspname NOT IN ('pg_catalog','information_schema')
),
mviews AS (
  SELECT schemaname AS schema, matviewname AS name FROM pg_matviews
)
SELECT json_build_object(
  'tables', COALESCE((SELECT json_agg(json_build_object(
       'schema', t.schema, 'name', t.name, 'bytes', t.bytes,
       'live_rows', t.live_rows,
       'columns', COALESCE(cols.columns, '[]'::json),
       'pk', COALESCE(pk.pk_cols, '[]'::json),
       'fks', COALESCE(fk.fks, '[]'::json))
     ORDER BY t.bytes DESC)
     FROM tabs t
     LEFT JOIN cols ON cols.table_schema=t.schema AND cols.table_name=t.name
     LEFT JOIN pk   ON pk.table_schema=t.schema   AND pk.table_name=t.name
     LEFT JOIN fk   ON fk.table_schema=t.schema   AND fk.table_name=t.name), '[]'::json),
  'matviews', COALESCE((SELECT json_agg(json_build_object('schema',schema,'name',name))
                        FROM mviews), '[]'::json),
  'enums', COALESCE((SELECT e FROM enums), '{}'::json)
) AS payload;
"""


# ---- Post-run DB-side metrics (read-only). Rendered in the UI after a load run. ----
# Each query returns rows the UI shows as its own table, so you see not just pgbench's
# throughput but what the load did INSIDE Postgres (writes, dead tuples, cache, locks).
METRICS = {
    "write_activity": """
        SELECT relname AS table, n_tup_ins AS inserts, n_tup_upd AS updates,
               n_tup_del AS deletes, n_live_tup AS live_rows, n_dead_tup AS dead_rows,
               to_char(coalesce(last_autovacuum,'epoch'),'HH24:MI:SS') AS last_autovac
        FROM pg_stat_user_tables
        WHERE n_tup_ins+n_tup_upd+n_tup_del > 0
        ORDER BY (n_tup_ins+n_tup_upd+n_tup_del) DESC LIMIT 15;""",
    "cache_hit": """
        SELECT 'index' AS kind,
               round(100.0*sum(idx_blks_hit)/NULLIF(sum(idx_blks_hit)+sum(idx_blks_read),0),2) AS hit_pct
        FROM pg_statio_user_indexes
        UNION ALL
        SELECT 'table',
               round(100.0*sum(heap_blks_hit)/NULLIF(sum(heap_blks_hit)+sum(heap_blks_read),0),2)
        FROM pg_statio_user_tables;""",
    "top_writes_by_time": """
        SELECT calls,
               round(total_exec_time::numeric,1) AS total_ms,
               round(mean_exec_time::numeric,2) AS mean_ms, rows,
               left(regexp_replace(query,'\\s+',' ','g'),70) AS query
        FROM pg_stat_statements
        WHERE query ILIKE 'UPDATE%' OR query ILIKE 'INSERT%' OR query ILIKE '%REFRESH%'
        ORDER BY total_exec_time DESC LIMIT 10;""",
    "current_locks": """
        SELECT mode, count(*) AS held
        FROM pg_locks WHERE granted GROUP BY mode ORDER BY 2 DESC LIMIT 10;""",
    "db_size": """
        SELECT pg_size_pretty(pg_database_size(current_database())) AS db_size,
               (SELECT count(*) FROM pg_stat_activity) AS connections;""",
}


def run(cmd, env=None, timeout=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, **(env or {})})
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except FileNotFoundError as e:
        return 127, "", f"command not found: {e}"


def introspect(url):
    """Run INTROSPECT_SQL against the given DB and return the parsed schema dict
    ({tables, matviews, enums}), or None on failure. Shared by /introspect,
    /validate and /run so generated SQL always reflects the CURRENTLY connected
    database — the kit is never tied to one schema."""
    rc, out, err = run(["psql", url, "-At", "-c", INTROSPECT_SQL], timeout=60)
    if rc != 0 or not out.strip():
        return None
    try:
        return json.loads(out.strip())
    except Exception:
        return None


def collect_metrics(url):
    """Run every METRICS query read-only and return {name: psql_table_text}.
    Best-effort: a failing query (e.g. pg_stat_statements not enabled) is reported
    inline rather than aborting the whole set."""
    result = {}
    for name, sql in METRICS.items():
        rc, out, err = run(["psql", url, "-P", "pager=off", "-c", sql], timeout=30)
        result[name] = out.strip() if rc == 0 and out.strip() else (err.strip() or "(no rows)")
    return result


def preflight(url, script_text, weight):
    """Validate one generated pgbench script BEFORE the run: strip pgbench meta
    (\\set / :vars), run the statement inside a transaction that is ROLLED BACK,
    so nothing is written but real errors (bad column, enum, FK, table) surface.
    Returns (ok, message). Weight 0 scripts are skipped."""
    if weight <= 0:
        return True, "skipped (weight 0)"
    stmt = []
    for line in script_text.splitlines():
        s = line.strip()
        if s.startswith("--") or s.startswith("\\") or not s:
            continue
        stmt.append(line)
    sql = "\n".join(stmt)
    # Replace pgbench :vars with harmless literals so the parser/planner is
    # exercised. Negative lookbehind so we don't corrupt "::type" casts.
    sql = re.sub(r"(?<!:):\w+", "1", sql)
    wrapped = "BEGIN;\n" + sql.rstrip().rstrip(";") + ";\nROLLBACK;"
    rc, out, err = run(["psql", url, "-v", "ON_ERROR_STOP=1", "-q", "-c", wrapped], timeout=20)
    if rc == 0:
        return True, "ok"
    # First ERROR line is the useful bit
    msg = next((l for l in err.splitlines() if "ERROR" in l), err.strip().splitlines()[0] if err.strip() else "failed")
    return False, msg


def qi(name):
    """Quote a SQL identifier safely (double-quote, escape embedded quotes)."""
    return '"' + str(name).replace('"', '""') + '"'


def synth_value(col, enums):
    """Generate a pgbench-safe SQL literal/expression for one column when
    building a generic INSERT. Type-driven so it works on ANY table:
      - enum         -> a real label from the enum
      - int/serial   -> random_zipfian-free random int
      - numeric/float-> random numeric
      - bool         -> random true/false
      - timestamp/date -> now()
      - uuid         -> gen_random_uuid()
      - json/jsonb   -> '{}'
      - text/varchar/char/bytea/other -> short random string
    FK columns are handled separately (subselect) before this is called.
    Returns a SQL expression string (already safe: no user text interpolated)."""
    udt = (col.get("udt") or "").lower()
    typ = (col.get("type") or "").lower()
    # enum: udt is the enum type name; pick its first label (a known-valid value)
    labels = enums.get(udt)
    if labels:
        return "'" + str(labels[0]).replace("'", "''") + "'"
    if typ in ("boolean",) or udt == "bool":
        return "(random() < 0.5)"
    if any(k in typ for k in ("timestamp", "date", "time")):
        return "now()"
    if "uuid" in typ or udt == "uuid":
        return "gen_random_uuid()"
    if "json" in typ:
        return "'{}'"
    if any(k in typ for k in ("integer", "bigint", "smallint", "serial")) \
       or udt in ("int2", "int4", "int8"):
        return "(floor(random()*1000000))::bigint"
    if any(k in typ for k in ("numeric", "decimal", "real", "double", "money")):
        return "round((random()*1000)::numeric, 2)"
    if "bytea" in typ:
        return "'\\x00'::bytea"
    # text / varchar / char / anything else: short random string
    return "substr(md5(random()::text), 1, 12)"


def gen_producer(m, schema):
    """Build a GENERIC INSERT for the job-queue table from live schema metadata,
    so the producer works on ANY table (not just a simple queue shape):
      - skip serial PK columns and columns that have a DEFAULT (let Postgres fill)
      - resolve FK columns to a random existing referenced id (keeps FKs valid)
      - synthesize every other NOT-NULL column by type
      - nullable, defaultless, non-FK columns are omitted (default NULL)
    Returns the INSERT SQL, or a harmless no-op if the table can't be introspected."""
    jt = qual(m["jobs_schema"], m["jobs_table"])
    tinfo = None
    for t in schema.get("tables", []):
        if t.get("schema") == m["jobs_schema"] and t.get("name") == m["jobs_table"]:
            tinfo = t
            break
    if not tinfo:
        return f"-- GENERATED: could not introspect {jt}; producer disabled.\nSELECT 1;\n"

    enums = schema.get("enums", {}) or {}
    pk = set(tinfo.get("pk") or [])
    # map fk column -> (ref_schema, ref_table, ref_column)
    fkmap = {}
    for fk in tinfo.get("fks", []):
        fkmap[fk["column"]] = (fk["ref_schema"], fk["ref_table"], fk["ref_column"])

    status_col = m.get("jobs_status")
    qval = (m.get("queued_value") or "").replace("'", "''")

    cols, vals = [], []
    for c in tinfo.get("columns", []):
        name = c["name"]
        # let the DB fill serial/identity PKs and any column with a default
        if c.get("has_default"):
            continue
        if name in pk and (c.get("has_default") or "serial" in (c.get("type") or "").lower()):
            continue
        # FK column: pick a random existing referenced row so the constraint holds
        if name in fkmap:
            rs, rt, rc = fkmap[name]
            cols.append(qi(name))
            vals.append(f"(SELECT {qi(rc)} FROM {qual(rs, rt)} ORDER BY random() LIMIT 1)")
            continue
        # status column: insert the "queued" value so the consumer has work to claim
        if status_col and name == status_col and qval:
            cols.append(qi(name)); vals.append(f"'{qval}'"); continue
        # NOT-NULL, no default, not FK: must synthesize a value
        if not c.get("nullable"):
            cols.append(qi(name)); vals.append(synth_value(c, enums))
        # nullable + no default + not FK: skip (defaults to NULL)
    if not cols:
        return f"-- GENERATED: {jt} has no insertable columns; producer disabled.\nSELECT 1;\n"
    collist = ", ".join(cols)
    vallist = ", ".join(vals)
    return (f"-- GENERATED by pg-load-kit. Generic producer (INSERT synthesized by column type/FK).\n"
            f"INSERT INTO {jt} ({collist})\nVALUES ({vallist});\n")


def gen_scripts(m, schema=None):
    """Write pgbench scripts from a mapping dict of real table/column names.
    Returns (paths, previews). If `schema` (the introspection payload) is given,
    the producer INSERT is synthesized generically from column metadata + FKs so
    it works on ANY table; otherwise it falls back to the simple queue-shape INSERT.
    Identifiers are quoted; no raw user text is interpolated unescaped."""
    os.makedirs(GEN, exist_ok=True)
    # Trim stray whitespace on every string field — a trailing space in a value
    # like "shipped " is a common paste error and would fail as an invalid enum.
    m = {k: (v.strip() if isinstance(v, str) else v) for k, v in m.items()}
    jt   = qual(m["jobs_schema"], m["jobs_table"])
    id_  = qi(m["jobs_id"])
    st   = qi(m["jobs_status"])
    lk   = qi(m["jobs_locked_at"])
    pl   = qi(m["jobs_payload"])
    cr   = qi(m["jobs_created_at"])
    qval = m["queued_value"].replace("'", "''")
    rval = m["running_value"].replace("'", "''")
    batch = int(m.get("batch", 10))

    bt   = qual(m["big_schema"], m["big_table"])
    bpk_col = m["big_pk"]
    bpl_col = (m.get("big_payload") or "").strip()
    bup_col = (m.get("big_updated_at") or "").strip()
    bup_is_ts = "timestamp" in (m.get("big_updated_at_type") or "").lower() \
                or "date" in (m.get("big_updated_at_type") or "").lower()
    maxpk = int(m["big_max_pk"])
    skew = float(m.get("zipfian_s", 1.1))

    # Build the SET clause defensively so we never emit invalid SQL:
    #  - only set updated_at=now() if it's a distinct, timestamp/date-typed column
    #  - only "touch" payload (self-assign) if it's a distinct column
    #  - if neither is usable, self-assign the PK — still writes a new row version
    #    (a real dead-tuple-producing write), which is all the load test needs.
    sets = []
    if bup_col and bup_col != bpk_col and bup_is_ts:
        sets.append(f"{qi(bup_col)} = now()")
    if bpl_col and bpl_col not in (bpk_col, bup_col):
        sets.append(f"{qi(bpl_col)} = {qi(bpl_col)}")
    if not sets:
        sets.append(f"{qi(bpk_col)} = {qi(bpk_col)}")
    set_clause = ", ".join(sets)

    consumer = f"""-- GENERATED by pg-load-kit control panel. Job-queue consumer (SKIP LOCKED).
\\set batch {batch}
UPDATE {jt}
   SET {st} = '{rval}', {lk} = now()
 WHERE {id_} IN (
   SELECT {id_} FROM {jt}
    WHERE {st} = '{qval}'
    ORDER BY {id_}
    FOR UPDATE SKIP LOCKED
    LIMIT :batch
 )
RETURNING {id_};
"""
    if schema is not None:
        # Generic INSERT synthesized from live column metadata + FKs — works on any table.
        producer = gen_producer(m, schema)
    else:
        # Fallback: simple queue-shape INSERT (payload, status, created).
        producer = f"""-- GENERATED by pg-load-kit control panel. Job-queue producer (INSERT).
INSERT INTO {jt} ({pl}, {st}, {cr})
VALUES (repeat('x', 200), '{qval}', now());
"""
    update = f"""-- GENERATED by pg-load-kit control panel. Integration-sync UPDATE (zipfian skew).
\\set id random_zipfian(1, {maxpk}, {skew})
UPDATE {bt} SET {set_clause} WHERE {qi(bpk_col)} = :id;
"""
    paths = {}
    for name, body in (("consumer", consumer), ("producer", producer), ("update", update)):
        p = os.path.join(GEN, f"{name}.sql")
        with open(p, "w") as f:
            f.write(body)
        paths[name] = p
    return paths, {"consumer": consumer, "producer": producer, "update": update}


def qual(schema, table):
    return qi(schema) + "." + qi(table)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, json.dumps({"error": "bad json"}))
        url = (data.get("db_url") or "").strip()
        if not url:
            return self._send(400, json.dumps({"error": "DATABASE_URL is required"}))

        if self.path == "/check":
            key = data.get("check", "overview")
            sql = CHECKS.get(key)
            if not sql:
                return self._send(400, json.dumps({"error": "unknown check"}))
            rc, out, err = run(["psql", url, "-P", "pager=off", "-c", sql], timeout=30)
            return self._send(200, json.dumps({"rc": rc, "out": out, "err": err}))

        if self.path == "/introspect":
            rc, out, err = run(["psql", url, "-At", "-c", INTROSPECT_SQL], timeout=60)
            if rc != 0:
                return self._send(200, json.dumps({"error": err.strip() or "introspection failed"}))
            try:
                schema = json.loads(out.strip())
            except Exception:
                return self._send(200, json.dumps({"error": "could not parse schema", "raw": out[:2000]}))
            schema["suggestion"] = suggest_mapping(schema)
            return self._send(200, json.dumps(schema))

        # (introspect() helper is reused by /validate and /run below.)

        if self.path == "/metrics":
            # On-demand read-only metrics pull (no load run needed).
            return self._send(200, json.dumps({"metrics": collect_metrics(url)}))

        if self.path == "/validate":
            # Generate scripts from the mapping and pre-flight each (rolled back) —
            # no load runs. Lets you confirm the mapping is correct before committing.
            mapping = data.get("mapping")
            if not mapping:
                return self._send(200, json.dumps({"error": "no mapping to validate"}))
            try:
                _, preview = gen_scripts(mapping, introspect(url))
            except (KeyError, ValueError) as e:
                return self._send(200, json.dumps({"error": f"bad schema mapping: {e}"}))
            checks = {
                "consumer.sql": (preview["consumer"], int(data.get("w_consumer", 6))),
                "producer.sql": (preview["producer"], int(data.get("w_producer", 2))),
                "update.sql":   (preview["update"],   int(data.get("w_update", 5))),
            }
            pf = {}
            for fn, (txt, w) in checks.items():
                ok, msg = preflight(url, txt, w)
                pf[fn] = ("ok" if ok else msg) if w > 0 else "skipped (weight 0)"
            return self._send(200, json.dumps({"preflight": pf, "scripts": preview}))

        if self.path == "/run":
            mapping = data.get("mapping")
            gen_env = {}
            preview = None
            if mapping:
                try:
                    paths, preview = gen_scripts(mapping, introspect(url))
                except (KeyError, ValueError) as e:
                    return self._send(200, json.dumps({"rc": 1, "out": "",
                        "err": f"bad schema mapping: {e}"}))
                gen_env = {
                    "CONSUMER_SQL": paths["consumer"],
                    "PRODUCER_SQL": paths["producer"],
                    "UPDATE_SQL": paths["update"],
                }
                if mapping.get("matview"):
                    gen_env["MVIEW"] = mapping["matview"]
            env = {
                "TARGET_DATABASE_URL": url,
                "I_UNDERSTAND_THIS_IS_NOT_PROD": "yes" if data.get("confirm") else "no",
                "CLIENTS": str(int(data.get("clients", 20))),
                "THREADS": str(int(data.get("threads", 4))),
                "DURATION": str(int(data.get("duration", 30))),
                "WARMUP": str(int(data.get("warmup", 10))),
                "W_CONSUMER": str(int(data.get("w_consumer", 6))),
                "W_PRODUCER": str(int(data.get("w_producer", 2))),
                "W_UPDATE": str(int(data.get("w_update", 5))),
                "RUN_MATVIEW": "1" if data.get("matview") else "0",
                **gen_env,
            }
            # Pre-flight: validate each generated statement against the DB (rolled
            # back) BEFORE running load, so schema mismatches surface as clear errors
            # instead of pgbench silently reporting "0 transactions processed".
            if preview and data.get("confirm") and env_int(env["DURATION"]) > 0:
                checks = {
                    "consumer.sql": (preview["consumer"], env_int(env["W_CONSUMER"])),
                    "producer.sql": (preview["producer"], env_int(env["W_PRODUCER"])),
                    "update.sql":   (preview["update"],   env_int(env["W_UPDATE"])),
                }
                pf, failed = {}, []
                for fn, (txt, w) in checks.items():
                    ok, msg = preflight(url, txt, w)
                    pf[fn] = ("ok" if ok else msg) if w > 0 else "skipped (weight 0)"
                    if not ok:
                        failed.append(f"{fn}: {msg}")
                if failed:
                    return self._send(200, json.dumps({
                        "rc": 1, "out": "", "preflight": pf, "scripts": preview,
                        "err": "Pre-flight failed — load NOT run. Fix the schema "
                               "mapping so the generated SQL matches your tables:\n  "
                               + "\n  ".join(failed)}))

            timeout = env_int(env["DURATION"]) + env_int(env["WARMUP"]) + 60
            rc, out, err = run(["bash", os.path.join(HERE, "run_load.sh")],
                               env=env, timeout=timeout)
            resp = {"rc": rc, "out": out, "err": err,
                    "summary": parse_pgbench(out)}
            if preview:
                resp["scripts"] = preview
            # Collect DB-side metrics after the run so the UI shows the full picture,
            # not just pgbench's throughput. Skipped for preview runs (duration 0).
            if data.get("confirm") and env_int(env["DURATION"]) > 0:
                resp["metrics"] = collect_metrics(url)
            return self._send(200, json.dumps(resp))

        self._send(404, json.dumps({"error": "unknown endpoint"}))


def suggest_mapping(schema):
    """Heuristically pick a job-queue table and a big UPDATE-target table, plus
    column roles, so the UI can pre-fill the dropdowns. Pure guesswork the user
    confirms/overrides — never authoritative."""
    tables = schema.get("tables", [])
    def cols(t):     return [c["name"] for c in t.get("columns", [])]
    def has(t, *ks): return [c for c in cols(t) if any(k in c.lower() for k in ks)]

    # job-queue candidate: has a status-like column + a locked/claimed-like column,
    # or a name that looks like a queue.
    def q_score(t):
        cs = cols(t); s = 0
        if has(t, "status", "state"): s += 3
        if has(t, "lock", "claim", "reserved", "picked"): s += 3
        if re.search(r"job|queue|task|work|outbox|event", t["name"], re.I): s += 2
        if has(t, "payload", "body", "data", "args"): s += 1
        return s
    jobs = max(tables, key=q_score, default=None)

    # big-table candidate: largest table (by bytes) that isn't the chosen queue.
    big = None
    for t in tables:            # tables already sorted by bytes desc from SQL
        if t is not jobs:
            big = t; break
    if big is None:
        big = jobs

    def pick(t, prefer, fallback_type=None):
        if not t: return None
        c = has(t, *prefer)
        if c: return c[0]
        if fallback_type:
            for col in t.get("columns", []):
                if fallback_type in col["type"].lower():
                    return col["name"]
        return (cols(t) or [None])[0]

    def pk_of(t):
        if not t: return None
        pk = t.get("pk") or []
        return pk[0] if pk else pick(t, ["id"], "int")

    mv = schema.get("matviews") or []
    return {
        "jobs_schema": (jobs or {}).get("schema"),
        "jobs_table":  (jobs or {}).get("name"),
        "jobs_id":     pk_of(jobs),
        "jobs_status": pick(jobs, ["status", "state"]),
        "jobs_locked_at": pick(jobs, ["lock", "claim", "reserved", "picked"], "timestamp"),
        "jobs_payload": pick(jobs, ["payload", "body", "data", "args"]),
        "jobs_created_at": pick(jobs, ["created", "inserted", "enqueued"], "timestamp"),
        "big_schema": (big or {}).get("schema"),
        "big_table":  (big or {}).get("name"),
        "big_pk":     pk_of(big),
        "big_payload": pick(big, ["payload", "body", "data", "value", "name"]),
        "big_updated_at": pick(big, ["updated", "modified", "changed"], "timestamp"),
        "big_live_rows": (big or {}).get("live_rows", 0),
        "matview": (mv[0]["schema"] + "." + mv[0]["name"]) if mv else "",
    }


def env_int(s):
    try: return int(s)
    except: return 0


def parse_pgbench(out):
    """Extract the headline numbers from pgbench stdout into a dict the UI shows
    as big stat cards. Also flags a network-bound run (high per-txn latency +
    long initial connection time = you're running far from the DB)."""
    def grab(pat, cast=float, default=None):
        m = re.search(pat, out)
        try: return cast(m.group(1)) if m else default
        except Exception: return default
    s = {
        "tps": grab(r"tps = ([\d.]+)"),
        "latency_ms": grab(r"latency average = ([\d.]+) ms"),
        "stddev_ms": grab(r"latency stddev = ([\d.]+) ms"),
        "transactions": grab(r"number of transactions actually processed: (\d+)", int),
        "clients": grab(r"number of clients: (\d+)", int),
        "duration_s": grab(r"duration: (\d+) s", int),
        "conn_time_ms": grab(r"initial connection time = ([\d.]+) ms"),
    }
    # Per-script breakdown
    scripts = []
    for m in re.finditer(r"SQL script \d+: (\S+).*?weight: (\d+).*?- (\d+) transactions.*?tps = ([\d.]+)",
                         out, re.S):
        scripts.append({"file": os.path.basename(m.group(1)), "weight": int(m.group(2)),
                        "transactions": int(m.group(3)), "tps": float(m.group(4))})
    s["scripts"] = scripts
    # Heuristic warning: network-bound rather than DB-bound
    notes = []
    if not s["transactions"]:
        notes.append("0 transactions processed — every statement errored. Check the "
                     "generated SQL matches your schema (table/column/enum values).")
    if s["latency_ms"] and s["latency_ms"] > 50 and s["tps"] and s["tps"] < 500:
        notes.append(f"High per-transaction latency ({s['latency_ms']:.0f} ms) with low tps "
                     f"({s['tps']:.0f}) suggests a NETWORK-bound run (client far from DB). "
                     "For a true capacity number, run this generator in the DB's region "
                     "(Heroku one-off dyno / same-region EC2).")
    if s["conn_time_ms"] and s["conn_time_ms"] > 3000:
        notes.append(f"Initial connection time was {s['conn_time_ms']/1000:.1f}s — high RTT "
                     "to the DB confirms the client is remote.")
    s["notes"] = notes
    return s


PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>pg-load-kit control panel</title>
<style>
 :root{--pp:#591b8c;--pp2:#7952b3;--ink:#24242e;--line:#e4e4e7;--acc:#be185d;--ok:#166534}
 *{box-sizing:border-box} body{font-family:-apple-system,Arial,sans-serif;margin:0;color:var(--ink);background:#faf8fd}
 header{background:var(--pp);color:#fff;padding:16px 24px} header h1{margin:0;font-size:18px}
 header p{margin:4px 0 0;font-size:12px;color:#d8c8ee}
 main{max-width:1150px;margin:20px auto;padding:0 20px;display:grid;grid-template-columns:1fr 1fr;gap:18px}
 .card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:16px}
 .card h2{margin:0 0 10px;font-size:14px;color:var(--pp)}
 label{display:block;font-size:12px;color:#52525b;margin:8px 0 3px}
 input,select{width:100%;padding:8px;border:1px solid var(--line);border-radius:6px;font-size:13px;background:#fff}
 .row{display:flex;gap:8px} .row>div{flex:1}
 button{background:var(--pp);color:#fff;border:0;border-radius:6px;padding:9px 14px;font-size:13px;cursor:pointer;margin-top:10px}
 button:hover{background:var(--pp2)} button.warn{background:var(--acc)} button.ghost{background:#efe9f7;color:var(--pp)}
 .full{grid-column:1/3}
 pre{background:#1e1e2e;color:#e4e4e7;padding:12px;border-radius:8px;overflow:auto;font-size:12px;max-height:420px;white-space:pre}
 .chk{background:#fef2f2;border:1px solid #f0a8a8;border-radius:6px;padding:8px 10px;font-size:12px;color:#7a1a1a;margin-top:8px}
 .muted{font-size:11px;color:#8a8a96} .pill{display:inline-block;font-size:11px;background:#f4f0fa;color:var(--pp);border-radius:20px;padding:2px 8px;margin-right:4px}
 .hide{display:none} .grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
 fieldset{border:1px solid var(--line);border-radius:8px;margin:12px 0 0;padding:10px}
 legend{font-size:12px;color:var(--pp);padding:0 6px;font-weight:600}
 .ok{color:var(--ok);font-size:12px}
 .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:10px}
 .stat{background:#f7f4fb;border:1px solid var(--line);border-radius:8px;padding:10px 12px}
 .stat .v{font-size:22px;font-weight:700;color:var(--pp)} .stat .k{font-size:11px;color:#6b7280;margin-top:2px}
 .note{background:#fff7ed;border:1px solid #fdba74;color:#7c2d12;border-radius:8px;padding:8px 10px;font-size:12px;margin:6px 0}
 .note.bad{background:#fef2f2;border-color:#f0a8a8;color:#7a1a1a}
 .mtable{margin:10px 0} .mtable h3{margin:0 0 4px;font-size:12px;color:var(--pp)}
 .mtable pre{max-height:220px;font-size:11px;margin:0}
 .help{font-size:11px;color:#6b7280;margin:2px 0 6px;line-height:1.35}
 .help code{background:#f0ecf7;padding:0 3px;border-radius:3px;font-size:10px}
 fieldset .grid2 .help{margin-top:2px}
 .steps{font-size:12px;color:#52525b;margin:6px 0 0;padding-left:18px} .steps li{margin:2px 0}
</style></head><body>
<header><h1>pg-load-kit — control panel</h1>
<p>Local only (127.0.0.1). Runs <b>psql</b>/<b>pgbench</b> against the DATABASE_URL you provide.</p></header>
<main>
 <div class="card full">
   <h2>1 · Connection</h2>
   <label>DATABASE_URL</label>
   <input id="db" placeholder="postgres://user:pass@host:5432/dbname?sslmode=require" autocomplete="off">
   <div class="help">Paste any Postgres connection string. Append <code>?sslmode=require</code> if it errors on SSL (common on Heroku). For <b>Check</b>/<b>Discover</b> you may use prod (read-only). For <b>Run load</b> use a <b>FORK/CLONE or DEV/UAT</b> only — never production.</div>
   <button class="ghost" onclick="discover()">🔍 Discover schema</button>
   <span id="dstat" class="muted"></span>
   <ol class="steps">
     <li><b>Discover schema</b> — reads this DB's tables/columns/keys and pre-fills a suggested mapping (section 2).</li>
     <li><b>Confirm the mapping</b> — fix the dropdowns/values (each field is explained inline).</li>
     <li><b>✓ Validate mapping</b> — confirms the SQL fits your schema; nothing is written.</li>
     <li><b>Generate &amp; run load</b> — tick the FORK/CLONE box, then run &amp; read the results below.</li>
   </ol>
 </div>

 <div class="card">
   <h2>Check data <span class="pill">read-only</span></h2>
   <label>Inspection</label>
   <select id="check">
     <option value="overview">Overview (size, version, connections)</option>
     <option value="write_mix">Write mix (pg_stat_statements)</option>
     <option value="table_sizes">Table sizes &amp; dead rows</option>
     <option value="activity">Active queries</option>
     <option value="locks">Blocking locks</option>
   </select>
   <button onclick="check()">Check data</button>
   <button class="ghost" onclick="refreshMetrics()">📊 Refresh metrics</button>
   <div class="muted">Metrics = write activity, cache hit, top queries, locks, size — pulled live, no load run needed.</div>
 </div>

 <div class="card">
   <h2>Run settings</h2>
   <p class="help">How hard and how long to push. These control the load intensity, not the SQL.</p>
   <div class="row"><div><label>Clients</label><input id="clients" type="number" value="20">
       <div class="help">Simultaneous connections. More = more contention. Start ~20.</div></div>
     <div><label>Threads</label><input id="threads" type="number" value="4">
       <div class="help">pgbench worker threads. Rule of thumb: clients ÷ 5.</div></div></div>
   <div class="row"><div><label>Duration (s)</label><input id="duration" type="number" value="30">
       <div class="help">Measured run length. Use 300+ to let autovacuum/bloat show up.</div></div>
     <div><label>Warmup (s)</label><input id="warmup" type="number" value="10">
       <div class="help">Discarded warm-up before measuring (cold cache). ~10–60.</div></div></div>
   <div class="row"><div><label>W consumer</label><input id="w_consumer" type="number" value="6">
       <div class="help">Weight: queue-claim traffic.</div></div>
     <div><label>W producer</label><input id="w_producer" type="number" value="2">
       <div class="help">Weight: enqueue INSERTs. <code>0</code> to skip.</div></div>
     <div><label>W update</label><input id="w_update" type="number" value="5">
       <div class="help">Weight: big-table UPDATEs.</div></div></div>
   <p class="help">Weights are <b>relative ratios</b> (6/2/5 ≈ 46% / 15% / 38%). Derive them from your real <code>pg_stat_statements</code> mix; set any to <code>0</code> to disable that pattern.</p>
   <label><input type="checkbox" id="matview" style="width:auto"> also refresh matview</label>
 </div>

 <div class="card full hide" id="mapcard">
   <h2>2 · Schema mapping <span class="pill">auto-detected — confirm or override</span></h2>
   <div class="muted" id="maphint"></div>
   <fieldset><legend>Job-queue table (SKIP LOCKED churn)</legend>
     <p class="help">Simulates workers claiming "to-do" rows and marking them done. Pick a table that has a <b>status/state column</b> (text or enum). If a table has no such column, it isn't a queue — use it as the big-update table below instead.</p>
     <div class="grid2">
       <div><label>Table</label><select id="jobs_table" onchange="onJobsTable()"></select>
         <div class="help">The table workers claim rows from (e.g. a jobs/orders/outbox table).</div></div>
       <div><label>ID / order column</label><select id="jobs_id"></select>
         <div class="help">Primary key used to order &amp; lock rows. Usually the <code>id</code> column.</div></div>
       <div><label>Status column</label><select id="jobs_status"></select>
         <div class="help"><b>Must be text/enum</b> (not an integer). Holds the workflow state, e.g. <code>status</code>.</div></div>
       <div><label>Locked-at column</label><select id="jobs_locked_at"></select>
         <div class="help">A timestamp column stamped when a row is claimed. Any spare timestamp works.</div></div>
       <div><label>Payload column</label><select id="jobs_payload"></select>
         <div class="help">A text column written by the producer INSERT. Not critical — any text column.</div></div>
       <div><label>Created-at column</label><select id="jobs_created_at"></select>
         <div class="help">Timestamp set on insert. Usually <code>created</code>/<code>created_at</code>.</div></div>
       <div><label>"queued" value</label><input id="queued_value" value="queued">
         <div class="help">A <b>real value already in the status column</b> meaning "waiting" (e.g. <code>pending</code>). No trailing spaces.</div></div>
       <div><label>"running" value</label><input id="running_value" value="running">
         <div class="help">A <b>real value</b> meaning "claimed/in-progress" (e.g. <code>shipped</code>). Must differ from the queued value.</div></div>
     </div>
   </fieldset>
   <fieldset><legend>Big UPDATE-target table (integration-sync volume)</legend>
     <p class="help">Simulates a sync process constantly UPDATEing rows in a large table. Pick your <b>biggest, most-written table</b> — this drives write volume, dead-row bloat, and autovacuum pressure.</p>
     <div class="grid2">
       <div><label>Table</label><select id="big_table" onchange="onBigTable()"></select>
         <div class="help">The large table to hammer with UPDATEs (usually the biggest by row count).</div></div>
       <div><label>PK column</label><select id="big_pk"></select>
         <div class="help">Primary key targeted by each UPDATE. Usually <code>id</code>.</div></div>
       <div><label>Payload column</label><select id="big_payload"></select>
         <div class="help">Optional column to "touch" on each UPDATE. Pick <code>(none)</code> if unsure.</div></div>
       <div><label>Updated-at column</label><select id="big_updated_at"></select>
         <div class="help">Optional timestamp set to <code>now()</code> on each UPDATE. <code>(none)</code> is fine.</div></div>
       <div><label>Max PK (zipfian range)</label><input id="big_max_pk" type="number" value="100000">
         <div class="help">The table's <b>max(pk)</b> — so random ids hit existing rows. Set to the real row count.</div></div>
       <div><label>Zipfian skew s (&gt;1 = hotter)</label><input id="zipfian_s" value="1.1">
         <div class="help">How concentrated the writes are. <code>1.1</code>=mild hotspot; higher=hotter few rows. Leave at 1.1.</div></div>
     </div>
   </fieldset>
   <fieldset><legend>Materialized view (optional)</legend>
     <p class="help">If set, the tool periodically refreshes this view <i>while</i> the write load runs — stresses heavy read+write together.</p>
     <div><label>Matview (schema.name)</label><select id="matview_sel"></select>
       <div class="help">Leave blank to skip. Needs a UNIQUE index for a concurrent (non-locking) refresh.</div></div>
   </fieldset>
   <div class="chk"><label style="margin:0"><input type="checkbox" id="confirm" style="width:auto">
     I confirm this URL is a <b>FORK/CLONE</b>, not production.</label></div>
   <button class="ghost" onclick="preview()">Preview SQL</button>
   <button class="ghost" onclick="validate()">✓ Validate mapping</button>
   <button class="warn" onclick="runLoad()">Generate &amp; run load</button>
   <p class="help"><b>Preview SQL</b>: see the generated statements (writes nothing). &nbsp;
   <b>✓ Validate mapping</b>: run each statement in a rolled-back transaction to confirm it matches your schema (writes nothing) — <b>always do this first</b>. &nbsp;
   <b>Generate &amp; run load</b>: run the real measured load (requires the FORK/CLONE box above).</p>
 </div>

 <div class="card full">
   <h2>Results</h2>
   <div id="stats" class="stats"></div>
   <div id="notes"></div>
   <div id="metrics"></div>
   <details id="rawwrap" open><summary style="cursor:pointer;font-size:12px;color:var(--pp);margin:8px 0">Raw log</summary>
   <pre id="out">Ready. Paste a DATABASE_URL and click "Discover schema".</pre></details>
 </div>
</main>
<script>
const $=id=>document.getElementById(id);
const out=t=>{$('out').textContent=t};
let SCHEMA=null;   // {tables:[...], matviews:[...], suggestion:{...}}

async function post(path,body){
  try{
    const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    return await r.json();
  }catch(e){out('Request failed: '+e);return null;}
}
function dburl(){const d=$('db').value.trim(); if(!d){out('Enter a DATABASE_URL first.');} return d;}

async function check(){
  const db=dburl(); if(!db)return; clearResults(); out('Working…');
  const j=await post('/check',{db_url:db,check:$('check').value}); if(!j)return;
  render(j);
}

async function refreshMetrics(){
  const db=dburl(); if(!db)return; clearResults(); out('Pulling metrics…');
  const j=await post('/metrics',{db_url:db}); if(!j)return;
  out('Metrics as of now (read-only).');
  render(j);   // render() knows how to draw j.metrics tables
}

async function discover(){
  const db=dburl(); if(!db)return;
  $('dstat').textContent='…discovering'; out('Introspecting schema…');
  const j=await post('/introspect',{db_url:db}); if(!j)return;
  if(j.error){$('dstat').textContent='';out('Discover failed: '+j.error);return;}
  SCHEMA=j;
  buildMapping(j);
  $('mapcard').classList.remove('hide');
  $('dstat').innerHTML=' <span class="ok">✓ found '+j.tables.length+' tables, '+j.matviews.length+' matviews</span>';
  out('Schema discovered. Review the auto-detected mapping in section 2, then "Generate & run".');
}

function opts(sel,list,val){sel.innerHTML='';list.forEach(v=>{const o=document.createElement('option');o.value=v;o.textContent=v;if(v===val)o.selected=true;sel.appendChild(o);});}
function tableNames(){return SCHEMA.tables.map(t=>t.schema+'.'+t.name);}
function tableByQName(q){return SCHEMA.tables.find(t=>t.schema+'.'+t.name===q);}
function colNames(q){const t=tableByQName(q);return t?t.columns.map(c=>c.name):[];}
function colType(q,name){const t=tableByQName(q);if(!t)return'';const c=t.columns.find(c=>c.name===name);return c?c.type:'';}

function buildMapping(j){
  const s=j.suggestion||{};
  const names=tableNames();
  opts($('jobs_table'),names,(s.jobs_schema&&s.jobs_table)?s.jobs_schema+'.'+s.jobs_table:names[0]);
  opts($('big_table'),names,(s.big_schema&&s.big_table)?s.big_schema+'.'+s.big_table:names[0]);
  onJobsTable(s); onBigTable(s);
  const mvs=['']. concat(j.matviews.map(m=>m.schema+'.'+m.name));
  opts($('matview_sel'),mvs,s.matview||'');
  if(s.jobs_status)$('jobs_status').value=s.jobs_status;
  if(s.big_live_rows)$('big_max_pk').value=Math.max(1,s.big_live_rows);
  $('maphint').textContent='Auto-detected: queue='+(s.jobs_table||'?')+', big='+(s.big_table||'?')+'. These are guesses — confirm the columns match your write pattern.';
}
function onJobsTable(s){
  const q=$('jobs_table').value, cols=colNames(q); s=s||{};
  opts($('jobs_id'),cols,s.jobs_id); opts($('jobs_status'),cols,s.jobs_status);
  opts($('jobs_locked_at'),cols,s.jobs_locked_at); opts($('jobs_payload'),cols,s.jobs_payload);
  opts($('jobs_created_at'),cols,s.jobs_created_at);
}
function onBigTable(s){
  const q=$('big_table').value, cols=colNames(q); s=s||{};
  const withNone=['(none)'].concat(cols);
  opts($('big_pk'),cols,s.big_pk);
  opts($('big_payload'),withNone,s.big_payload||'(none)');
  opts($('big_updated_at'),withNone,s.big_updated_at||'(none)');
}

function mapping(){
  const jt=tableByQName($('jobs_table').value), bt=tableByQName($('big_table').value);
  return {
    jobs_schema:jt.schema, jobs_table:jt.name,
    jobs_id:$('jobs_id').value, jobs_status:$('jobs_status').value,
    jobs_locked_at:$('jobs_locked_at').value, jobs_payload:$('jobs_payload').value,
    jobs_created_at:$('jobs_created_at').value,
    queued_value:$('queued_value').value, running_value:$('running_value').value,
    big_schema:bt.schema, big_table:bt.name,
    big_pk:$('big_pk').value,
    big_payload:($('big_payload').value==='(none)'?'':$('big_payload').value),
    big_updated_at:($('big_updated_at').value==='(none)'?'':$('big_updated_at').value),
    big_updated_at_type:colType($('big_table').value,$('big_updated_at').value),
    big_max_pk:+$('big_max_pk').value, zipfian_s:$('zipfian_s').value,
    matview:$('matview_sel').value
  };
}

async function preview(){
  const db=dburl(); if(!db)return; clearResults(); out('Generating SQL preview…');
  // preview runs with duration 0 so nothing executes; server still returns scripts
  const j=await post('/run',{db_url:db,confirm:false,mapping:mapping(),
    clients:1,threads:1,duration:0,warmup:0,matview:false}); if(!j)return;
  if(j.scripts){
    out('--- consumer.sql ---\n'+j.scripts.consumer+'\n--- producer.sql ---\n'+j.scripts.producer+'\n--- update.sql ---\n'+j.scripts.update+'\n(Preview only — Run load refused because FORK/CLONE not confirmed.)');
  }else render(j);
}

async function validate(){
  const db=dburl(); if(!db)return; clearResults(); out('Validating mapping (rolled back — nothing written)…');
  const j=await post('/validate',{db_url:db,mapping:mapping(),
    w_consumer:+$('w_consumer').value,w_producer:+$('w_producer').value,w_update:+$('w_update').value});
  if(!j)return;
  if(j.error){out('ERROR: '+j.error);return;}
  const allok=Object.values(j.preflight||{}).every(v=>v==='ok'||/skipped/.test(v));
  out(allok?'✅ Mapping is valid — safe to run load.':'❌ Mapping has errors — fix before running (see banners above).');
  render(j);
}

async function runLoad(){
  const db=dburl(); if(!db)return;
  if(!$('confirm').checked){out('Tick the FORK/CLONE confirmation before running load.');return;}
  out('Running load… (this blocks for ~duration+warmup seconds)');
  const j=await post('/run',{db_url:db,confirm:true,mapping:mapping(),
    clients:+$('clients').value,threads:+$('threads').value,
    duration:+$('duration').value,warmup:+$('warmup').value,
    w_consumer:+$('w_consumer').value,w_producer:+$('w_producer').value,w_update:+$('w_update').value,
    matview:$('matview').checked}); if(!j)return;
  render(j);
}

function clearResults(){$('stats').innerHTML='';$('notes').innerHTML='';$('metrics').innerHTML='';}
function esc(t){return (t||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

function render(j){
  clearResults();
  if(j.error){out('ERROR: '+j.error);return;}
  let s=(j.out||'');
  if(j.err){s+='\n--- stderr ---\n'+j.err;}
  if(typeof j.rc!=='undefined')s+='\n[exit '+j.rc+']';
  out(s.trim()||'(no output)');

  // Pre-flight validation results (per generated script) — shown as banners
  if(j.preflight){
    for(const fn in j.preflight){
      const v=j.preflight[fn], bad=!(v==='ok'||/skipped/.test(v));
      $('notes').innerHTML+=`<div class="note ${bad?'bad':''}">${bad?'❌':'✅'} <b>${fn}</b>: ${esc(v)}</div>`;
    }
  }

  // Headline stat cards from the parsed pgbench summary
  const sm=j.summary;
  if(sm){
    const cards=[
      ['tps', sm.tps!=null?sm.tps.toFixed(0):'—', 'transactions/sec'],
      ['latency', sm.latency_ms!=null?sm.latency_ms.toFixed(1)+' ms':'—', 'avg per txn'],
      ['stddev', sm.stddev_ms!=null?sm.stddev_ms.toFixed(1)+' ms':'—', 'latency jitter'],
      ['txns', sm.transactions!=null?sm.transactions.toLocaleString():'—', 'processed'],
      ['clients', sm.clients??'—', 'concurrent'],
      ['connect', sm.conn_time_ms!=null?(sm.conn_time_ms/1000).toFixed(1)+' s':'—', 'initial conn time'],
    ];
    $('stats').innerHTML=cards.map(c=>`<div class="stat"><div class="v">${c[1]}</div><div class="k">${c[0]} · ${c[2]}</div></div>`).join('');
    (sm.notes||[]).forEach(n=>{
      const bad=/0 transactions/.test(n);
      $('notes').innerHTML+=`<div class="note ${bad?'bad':''}">${bad?'❌ ':'⚠️ '}${esc(n)}</div>`;
    });
    if(sm.scripts&&sm.scripts.length){
      const rows=sm.scripts.map(x=>`${x.file.padEnd(16)} w=${x.weight}  ${String(x.transactions).padStart(8)} txns   ${x.tps.toFixed(1)} tps`).join('\n');
      $('metrics').innerHTML+=`<div class="mtable"><h3>Per-script breakdown</h3><pre>${esc(rows)}</pre></div>`;
    }
  }
  // DB-side metrics tables (write activity, cache hit, top queries, locks, size)
  if(j.metrics){
    const titles={write_activity:'Write activity (inserts/updates/deletes + dead rows)',
      cache_hit:'Cache hit ratio (want >99%)', top_writes_by_time:'Top write queries by total time (pg_stat_statements)',
      current_locks:'Locks currently held', db_size:'Database size & connections'};
    for(const k of ['write_activity','cache_hit','top_writes_by_time','current_locks','db_size']){
      if(j.metrics[k]!=null)
        $('metrics').innerHTML+=`<div class="mtable"><h3>${titles[k]||k}</h3><pre>${esc(j.metrics[k])}</pre></div>`;
    }
  }
}
</script></body></html>"""


if __name__ == "__main__":
    print(f"pg-load-kit control panel → http://{HOST}:{PORT}")
    print("psql:", "found" if run(["psql","--version"])[0]==0 else "MISSING",
          "| pgbench:", "found" if run(["pgbench","--version"])[0]==0 else "MISSING")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
