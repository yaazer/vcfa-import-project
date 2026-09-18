"""Durable run state.

A campaign of ~1800 VMs runs for days across many sessions, so every decision
the tool makes is written to SQLite immediately. Killing the process mid-wave
and re-running `status` or `run` must always pick up exactly where it left off.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .folders import folder_matches
from .inventory import VmRecord

# --- VM lifecycle -----------------------------------------------------------
S_PENDING = "pending"                    # in inventory, nothing done yet
S_PRECHECK_RUNNING = "precheck_running"
S_PRECHECK_PASSED = "precheck_passed"
S_PRECHECK_FAILED = "precheck_failed"
S_IMPORTING = "importing"
S_AWAITING_COMMIT = "awaiting_commit"    # commit_action = Wait, needs approval
S_COMMITTED = "committed"                # terminal success
S_FAILED = "failed"                      # terminal failure (retries exhausted)
S_ROLLING_BACK = "rolling_back"          # rollbackAction requested, operator reverting
S_ROLLED_BACK = "rolled_back"            # ownership back with vCenter; retryable
S_SKIPPED = "skipped"                    # excluded by the operator

TERMINAL_STATES = (S_COMMITTED, S_FAILED, S_ROLLED_BACK, S_SKIPPED)
ACTIVE_STATES = (S_PRECHECK_RUNNING, S_IMPORTING)

# --- Batch lifecycle ---------------------------------------------------------
B_PLANNED = "planned"
B_APPLIED = "applied"
B_RUNNING = "running"
B_SUCCEEDED = "succeeded"
B_PARTIAL = "partial"
B_FAILED = "failed"
B_TIMEDOUT = "timedout"
B_ROLLING_BACK = "rolling_back"
B_ROLLED_BACK = "rolled_back"
B_DELETED = "deleted"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS vms (
    moref          TEXT PRIMARY KEY,
    vm_name        TEXT NOT NULL DEFAULT '',
    namespace      TEXT NOT NULL,
    mode           TEXT NOT NULL DEFAULT 'preserve',
    wave           INTEGER NOT NULL DEFAULT 1,
    grp            TEXT NOT NULL DEFAULT '',
    nics_json      TEXT NOT NULL DEFAULT '[]',
    notes          TEXT NOT NULL DEFAULT '',
    src_row        INTEGER NOT NULL DEFAULT 0,
    state          TEXT NOT NULL DEFAULT 'pending',
    attempts       INTEGER NOT NULL DEFAULT 0,
    batch_name     TEXT,
    precheck_batch TEXT,
    operation_name TEXT,
    last_phase     TEXT,
    message        TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vms_state ON vms(state);
CREATE INDEX IF NOT EXISTS idx_vms_wave  ON vms(wave);
CREATE INDEX IF NOT EXISTS idx_vms_ns    ON vms(namespace);

CREATE TABLE IF NOT EXISTS batches (
    name          TEXT NOT NULL,
    namespace     TEXT NOT NULL,
    run_id        TEXT NOT NULL DEFAULT '',
    wave          INTEGER NOT NULL DEFAULT 1,
    stage         TEXT NOT NULL DEFAULT 'import',
    state         TEXT NOT NULL DEFAULT 'planned',
    vm_count      INTEGER NOT NULL DEFAULT 0,
    manifest_path TEXT,
    message       TEXT,
    created_at    TEXT NOT NULL,
    applied_at    TEXT,
    finished_at   TEXT,
    PRIMARY KEY (name, namespace)
);
CREATE INDEX IF NOT EXISTS idx_batches_state ON batches(state);

CREATE TABLE IF NOT EXISTS transitions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    moref      TEXT NOT NULL,
    vm_name    TEXT NOT NULL DEFAULT '',
    namespace  TEXT NOT NULL DEFAULT '',
    from_state TEXT,
    to_state   TEXT NOT NULL,
    stage      TEXT,
    batch      TEXT,
    operation  TEXT,
    phase      TEXT,
    message    TEXT,
    wave       INTEGER,
    attempt    INTEGER,
    held_secs  REAL
);
CREATE INDEX IF NOT EXISTS idx_trans_moref ON transitions(moref);
CREATE INDEX IF NOT EXISTS idx_trans_ts    ON transitions(ts);

CREATE TABLE IF NOT EXISTS discovered (
    moref         TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    power_state   TEXT NOT NULL DEFAULT '',
    cpu_count     INTEGER NOT NULL DEFAULT 0,
    memory_mb     INTEGER NOT NULL DEFAULT 0,
    folder        TEXT NOT NULL DEFAULT '',
    cluster       TEXT NOT NULL DEFAULT '',
    host          TEXT NOT NULL DEFAULT '',
    guest_os      TEXT NOT NULL DEFAULT '',
    tools_status  TEXT NOT NULL DEFAULT '',
    nics_json     TEXT NOT NULL DEFAULT '[]',
    networks      TEXT NOT NULL DEFAULT '',
    selected      INTEGER NOT NULL DEFAULT 0,
    namespace     TEXT NOT NULL DEFAULT '',
    wave          INTEGER NOT NULL DEFAULT 0,   -- 0 = not set; the map or --wave decides
    notes         TEXT NOT NULL DEFAULT '',
    discovered_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_disc_selected ON discovered(selected);
CREATE INDEX IF NOT EXISTS idx_disc_cluster  ON discovered(cluster);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    level    TEXT NOT NULL DEFAULT 'info',
    moref    TEXT,
    batch    TEXT,
    message  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not add
# them to a database that already exists, so _migrate() does it explicitly.
ADDED_COLUMNS = {
    "vms": [
        ("src_vcenter", "TEXT NOT NULL DEFAULT ''"),
        ("src_cluster", "TEXT NOT NULL DEFAULT ''"),
        ("src_folder", "TEXT NOT NULL DEFAULT ''"),
        ("src_host", "TEXT NOT NULL DEFAULT ''"),
        ("src_networks", "TEXT NOT NULL DEFAULT ''"),
        ("src_power", "TEXT NOT NULL DEFAULT ''"),
        ("src_cpu", "INTEGER NOT NULL DEFAULT 0"),
        ("src_memory_mb", "INTEGER NOT NULL DEFAULT 0"),
        ("src_tools", "TEXT NOT NULL DEFAULT ''"),
        ("target_resource", "TEXT NOT NULL DEFAULT ''"),
        ("precheck_started_at", "TEXT"),
        ("precheck_finished_at", "TEXT"),
        ("import_started_at", "TEXT"),
        ("committed_at", "TEXT"),
        ("rolled_back_at", "TEXT"),
        ("src_datacenter", "TEXT NOT NULL DEFAULT ''"),
    ],
    "discovered": [
        ("datacenter", "TEXT NOT NULL DEFAULT ''"),
    ],
}

# When a VM reaches one of these states, stamp the matching column.
STATE_TIMESTAMPS = {
    S_PRECHECK_RUNNING: "precheck_started_at",
    S_PRECHECK_PASSED: "precheck_finished_at",
    S_PRECHECK_FAILED: "precheck_finished_at",
    S_IMPORTING: "import_started_at",
    S_COMMITTED: "committed_at",
    S_ROLLED_BACK: "rolled_back_at",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


class Store:
    def __init__(self, path: str, ledger_path: Optional[str] = None):
        self.path = str(Path(path).expanduser())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # The ledger is an append-only JSONL mirror of the transitions table. It
        # survives the database being deleted or rebuilt, and can be shipped
        # straight to a ticket, a SIEM, or a change record.
        self.ledger_path = str(
            Path(ledger_path).expanduser() if ledger_path
            else Path(self.path).parent / "ledger.jsonl"
        )
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        with closing(self.conn.cursor()) as cur:
            cur.executescript(SCHEMA)
        self.conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        for table, columns in ADDED_COLUMNS.items():
            existing = {r["name"] for r in
                        self.conn.execute("PRAGMA table_info({})".format(table)).fetchall()}
            for name, decl in columns:
                if name not in existing:
                    self.conn.execute(
                        "ALTER TABLE {} ADD COLUMN {} {}".format(table, name, decl))
        self.conn.commit()

    def _append_ledger(self, entry: Dict[str, Any]) -> None:
        try:
            Path(self.ledger_path).parent.mkdir(parents=True, exist_ok=True)
            with open(self.ledger_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
        except OSError:
            # The ledger is a convenience mirror; never fail a migration over it.
            pass

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ meta
    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        self.conn.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    # ------------------------------------------------------------- vms
    def sync_inventory(self, records: Sequence[VmRecord]) -> Dict[str, Any]:
        """Merge the inventory into the store without losing progress.

        New rows are inserted as pending. Rows already in a terminal or active
        state keep that state; their target definition is only refreshed while
        they are still pending, so a mid-flight VM can never have its target
        silently changed underneath it.
        """
        summary = {"added": 0, "updated": 0, "unchanged": 0, "locked": 0, "conflicts": []}
        for rec in records:
            existing = self.get_vm(rec.moref)
            nics_json = json.dumps([n.to_dict() for n in rec.nics])
            if existing is None:
                self.conn.execute(
                    "INSERT INTO vms(moref, vm_name, namespace, mode, wave, grp, nics_json, notes,"
                    " src_row, state, created_at, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rec.moref, rec.vm_name, rec.namespace, rec.mode, rec.wave,
                     rec.group, nics_json, rec.notes, rec.row, S_PENDING, _now(), _now()),
                )
                self._queued_transition(rec)
                self._provenance_from_discovery(rec.moref)
                summary["added"] += 1
                continue

            changed = (
                existing["namespace"] != rec.namespace
                or existing["nics_json"] != nics_json
                or existing["mode"] != rec.mode
                or existing["wave"] != rec.wave
                or existing["grp"] != rec.group
            )
            if not changed:
                summary["unchanged"] += 1
                continue
            if existing["state"] in (S_PENDING, S_PRECHECK_FAILED, S_FAILED, S_SKIPPED):
                self.conn.execute(
                    "UPDATE vms SET vm_name=?, namespace=?, mode=?, wave=?, grp=?, nics_json=?,"
                    " notes=?, src_row=?, updated_at=? WHERE moref=?",
                    (rec.vm_name, rec.namespace, rec.mode, rec.wave, rec.group,
                     nics_json, rec.notes, rec.row, _now(), rec.moref),
                )
                summary["updated"] += 1
            else:
                summary["locked"] += 1
                summary["conflicts"].append(
                    "{} ({}) is {} -- inventory change ignored".format(
                        rec.moref, rec.vm_name, existing["state"])
                )
        self.conn.commit()
        return summary

    def _queued_transition(self, rec: VmRecord) -> None:
        """The first ledger entry for a VM: the moment it entered the campaign."""
        ts = _now()
        entry = {
            "ts": ts, "moref": rec.moref, "vm_name": rec.vm_name, "namespace": rec.namespace,
            "from_state": None, "to_state": S_PENDING, "stage": "queue", "batch": None,
            "operation": None, "phase": None, "message": "queued for import",
            "wave": rec.wave, "attempt": 0, "held_secs": None,
        }
        self.conn.execute(
            "INSERT INTO transitions(ts, moref, vm_name, namespace, from_state, to_state,"
            " stage, batch, operation, phase, message, wave, attempt, held_secs)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, rec.moref, rec.vm_name, rec.namespace, None, S_PENDING, "queue", None,
             None, None, "queued for import", rec.wave, 0, None),
        )
        self._append_ledger(entry)

    def _provenance_from_discovery(self, moref: str) -> None:
        """Copy the vCenter facts onto the VM row so the record stands alone."""
        row = self.conn.execute(
            "SELECT * FROM discovered WHERE moref=?", (moref,)).fetchone()
        if row is None:
            return
        self.conn.execute(
            "UPDATE vms SET src_vcenter=?, src_cluster=?, src_folder=?, src_host=?,"
            " src_networks=?, src_power=?, src_cpu=?, src_memory_mb=?, src_tools=?,"
            " src_datacenter=? WHERE moref=?",
            (str(self.get_meta("vcenter") or ""), row["cluster"], row["folder"], row["host"],
             row["networks"], row["power_state"], row["cpu_count"], row["memory_mb"],
             row["tools_status"], row["datacenter"], moref),
        )

    def get_vm(self, moref: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM vms WHERE moref=?", (moref,)).fetchone()

    def query_vms(
        self,
        states: Optional[Sequence[str]] = None,
        wave: Optional[int] = None,
        namespace: Optional[str] = None,
        morefs: Optional[Sequence[str]] = None,
        batch_name: Optional[str] = None,
        limit: Optional[int] = None,
        order: str = "wave, namespace, grp, vm_name",
        folders: Optional[Sequence[str]] = None,
        folder_exact: bool = False,
    ) -> List[sqlite3.Row]:
        """Query queued VMs. `folders` scopes to source-folder subtrees (see folders.py)."""
        sql = "SELECT * FROM vms WHERE 1=1"
        params: List[Any] = []
        if states:
            sql += " AND state IN ({})".format(",".join("?" * len(states)))
            params += list(states)
        if wave is not None:
            sql += " AND wave=?"
            params.append(wave)
        if namespace:
            sql += " AND namespace=?"
            params.append(namespace)
        if morefs:
            sql += " AND moref IN ({})".format(",".join("?" * len(morefs)))
            params += list(morefs)
        if batch_name:
            sql += " AND (batch_name=? OR precheck_batch=?)"
            params += [batch_name, batch_name]
        sql += " ORDER BY " + order
        if limit and not folders:
            sql += " LIMIT {}".format(int(limit))
        rows = list(self.conn.execute(sql, params).fetchall())
        if folders:
            # Subtree semantics live in Python; the limit is applied afterwards.
            rows = [r for r in rows
                    if folder_matches(r["src_folder"] or "", folders, exact=folder_exact)]
            if limit:
                rows = rows[:int(limit)]
        return rows

    def set_vm_state(
        self,
        moref: str,
        state: str,
        *,
        message: Optional[str] = None,
        phase: Optional[str] = None,
        batch_name: Optional[str] = None,
        precheck_batch: Optional[str] = None,
        operation_name: Optional[str] = None,
        target_resource: Optional[str] = None,
        stage: Optional[str] = None,
        bump_attempts: bool = False,
    ) -> None:
        """Move a VM to a new state, recording the transition.

        Every state change is written to the transitions table and mirrored to
        the append-only ledger, so the full history of each VM is recoverable
        long after the campaign ends.
        """
        before = self.get_vm(moref)
        if before is None:
            return
        now = _now()

        sets = ["state=?", "updated_at=?"]
        params: List[Any] = [state, now]
        for column, value in (
            ("message", message),
            ("last_phase", phase),
            ("batch_name", batch_name),
            ("precheck_batch", precheck_batch),
            ("operation_name", operation_name),
            ("target_resource", target_resource),
        ):
            if value is not None:
                sets.append("{}=?".format(column))
                params.append(value if value != "" else None)
        if bump_attempts:
            sets.append("attempts=attempts+1")

        # Stamp the milestone columns the first time each is reached.
        stamp = STATE_TIMESTAMPS.get(state)
        if stamp and before["state"] != state:
            sets.append("{}=?".format(stamp))
            params.append(now)

        params.append(moref)
        self.conn.execute("UPDATE vms SET {} WHERE moref=?".format(", ".join(sets)), params)

        if before["state"] != state:
            self._record_transition(
                before, state, now,
                stage=stage,
                batch=batch_name or precheck_batch or before["batch_name"],
                operation=operation_name or before["operation_name"],
                phase=phase,
                message=message,
                bump_attempts=bump_attempts,
            )
        self.conn.commit()

    def _record_transition(
        self,
        before: sqlite3.Row,
        to_state: str,
        ts: str,
        *,
        stage: Optional[str] = None,
        batch: Optional[str] = None,
        operation: Optional[str] = None,
        phase: Optional[str] = None,
        message: Optional[str] = None,
        bump_attempts: bool = False,
    ) -> None:
        # How long the VM sat in the state it is leaving.
        start = _parse_ts(before["updated_at"]) or _parse_ts(before["created_at"])
        end = _parse_ts(ts)
        held = (end - start).total_seconds() if (start and end) else None
        attempt = before["attempts"] + (1 if bump_attempts else 0)

        entry = {
            "ts": ts,
            "moref": before["moref"],
            "vm_name": before["vm_name"],
            "namespace": before["namespace"],
            "from_state": before["state"],
            "to_state": to_state,
            "stage": stage,
            "batch": batch,
            "operation": operation,
            "phase": phase,
            "message": (message or "")[:2000] or None,
            "wave": before["wave"],
            "attempt": attempt,
            "held_secs": round(held, 1) if held is not None else None,
        }
        self.conn.execute(
            "INSERT INTO transitions(ts, moref, vm_name, namespace, from_state, to_state,"
            " stage, batch, operation, phase, message, wave, attempt, held_secs)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (entry["ts"], entry["moref"], entry["vm_name"], entry["namespace"],
             entry["from_state"], entry["to_state"], entry["stage"], entry["batch"],
             entry["operation"], entry["phase"], entry["message"], entry["wave"],
             entry["attempt"], entry["held_secs"]),
        )
        self._append_ledger(entry)

    def transitions(
        self,
        moref: Optional[str] = None,
        limit: Optional[int] = None,
        to_state: Optional[str] = None,
        since: Optional[str] = None,
        newest_first: bool = False,
    ) -> List[sqlite3.Row]:
        sql = "SELECT * FROM transitions WHERE 1=1"
        params: List[Any] = []
        if moref:
            sql += " AND moref=?"
            params.append(moref)
        if to_state:
            sql += " AND to_state=?"
            params.append(to_state)
        if since:
            sql += " AND ts >= ?"
            params.append(since)
        sql += " ORDER BY id DESC" if newest_first else " ORDER BY id ASC"
        if limit:
            sql += " LIMIT {}".format(int(limit))
        return list(self.conn.execute(sql, params).fetchall())

    def transition_stats(self) -> Dict[str, Any]:
        """Throughput and duration figures for the campaign so far."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM transitions"
        ).fetchone()
        out: Dict[str, Any] = {
            "transitions": row["n"], "first": row["first_ts"], "last": row["last_ts"]}

        durations = self.conn.execute(
            "SELECT AVG(d) AS avg_d, MIN(d) AS min_d, MAX(d) AS max_d, COUNT(*) AS n FROM ("
            "  SELECT (julianday(committed_at) - julianday(import_started_at)) * 86400.0 AS d"
            "  FROM vms WHERE committed_at IS NOT NULL AND import_started_at IS NOT NULL)"
        ).fetchone()
        if durations and durations["n"]:
            out["import_seconds"] = {
                "count": durations["n"],
                "avg": round(durations["avg_d"] or 0, 1),
                "min": round(durations["min_d"] or 0, 1),
                "max": round(durations["max_d"] or 0, 1),
            }
        return out

    def attach_provenance(self, moref: str, source: Dict[str, Any]) -> None:
        """Record where a VM came from, captured at the moment it was queued."""
        self.conn.execute(
            "UPDATE vms SET src_vcenter=?, src_cluster=?, src_folder=?, src_host=?,"
            " src_networks=?, src_power=?, src_cpu=?, src_memory_mb=?, src_tools=?"
            " WHERE moref=?",
            (source.get("vcenter", ""), source.get("cluster", ""), source.get("folder", ""),
             source.get("host", ""), source.get("networks", ""), source.get("power_state", ""),
             int(source.get("cpu_count") or 0), int(source.get("memory_mb") or 0),
             source.get("tools_status", ""), moref),
        )
        self.conn.commit()

    def set_many_states(self, morefs: Iterable[str], state: str, **kwargs) -> int:
        count = 0
        for moref in morefs:
            self.set_vm_state(moref, state, **kwargs)
            count += 1
        return count

    def counts(self, wave: Optional[int] = None) -> Dict[str, int]:
        sql = "SELECT state, COUNT(*) AS n FROM vms"
        params: List[Any] = []
        if wave is not None:
            sql += " WHERE wave=?"
            params.append(wave)
        sql += " GROUP BY state"
        return {r["state"]: r["n"] for r in self.conn.execute(sql, params).fetchall()}

    def waves(self) -> List[int]:
        rows = self.conn.execute("SELECT DISTINCT wave FROM vms ORDER BY wave").fetchall()
        return [r["wave"] for r in rows]

    def namespaces(self) -> List[str]:
        rows = self.conn.execute("SELECT DISTINCT namespace FROM vms ORDER BY namespace").fetchall()
        return [r["namespace"] for r in rows]

    def subnets(self) -> List[Dict[str, str]]:
        """Distinct (namespace, subnet, kind, apiGroup) tuples referenced by the inventory."""
        out: Dict[str, Dict[str, str]] = {}
        for row in self.conn.execute("SELECT namespace, nics_json FROM vms").fetchall():
            for nic in json.loads(row["nics_json"]):
                if not nic.get("subnet"):
                    continue
                key = "{}|{}".format(row["namespace"], nic["subnet"])
                out[key] = {
                    "namespace": row["namespace"],
                    "subnet": nic["subnet"],
                    "kind": nic.get("subnet_kind", "Subnet"),
                    "api_group": nic.get("subnet_api_group", ""),
                }
        return sorted(out.values(), key=lambda d: (d["namespace"], d["subnet"]))

    # --------------------------------------------------------- batches
    def create_batch(
        self,
        name: str,
        namespace: str,
        run_id: str,
        wave: int,
        stage: str,
        vm_count: int,
        manifest_path: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO batches(name, namespace, run_id, wave, stage, state, vm_count,"
            " manifest_path, created_at) VALUES(?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(name, namespace) DO UPDATE SET"
            " run_id=excluded.run_id, wave=excluded.wave, stage=excluded.stage,"
            " vm_count=excluded.vm_count, manifest_path=excluded.manifest_path",
            (name, namespace, run_id, wave, stage, B_PLANNED, vm_count, manifest_path, _now()),
        )
        self.conn.commit()

    def set_batch_state(
        self,
        name: str,
        namespace: str,
        state: str,
        message: Optional[str] = None,
    ) -> None:
        sets = ["state=?"]
        params: List[Any] = [state]
        if message is not None:
            sets.append("message=?")
            params.append(message)
        if state == B_APPLIED:
            sets.append("applied_at=?")
            params.append(_now())
        if state in (B_SUCCEEDED, B_PARTIAL, B_FAILED, B_TIMEDOUT, B_ROLLED_BACK, B_DELETED):
            sets.append("finished_at=?")
            params.append(_now())
        params += [name, namespace]
        self.conn.execute(
            "UPDATE batches SET {} WHERE name=? AND namespace=?".format(", ".join(sets)), params
        )
        self.conn.commit()

    def get_batch(self, name: str, namespace: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM batches WHERE name=? AND namespace=?", (name, namespace)
        ).fetchone()

    def query_batches(
        self,
        states: Optional[Sequence[str]] = None,
        stage: Optional[str] = None,
        run_id: Optional[str] = None,
        wave: Optional[int] = None,
    ) -> List[sqlite3.Row]:
        sql = "SELECT * FROM batches WHERE 1=1"
        params: List[Any] = []
        if states:
            sql += " AND state IN ({})".format(",".join("?" * len(states)))
            params += list(states)
        if stage:
            sql += " AND stage=?"
            params.append(stage)
        if run_id:
            sql += " AND run_id=?"
            params.append(run_id)
        if wave is not None:
            sql += " AND wave=?"
            params.append(wave)
        sql += " ORDER BY wave, namespace, name"
        return list(self.conn.execute(sql, params).fetchall())

    # ------------------------------------------------------ discovered
    def upsert_discovered(self, vms: Sequence[Any]) -> Dict[str, int]:
        """Merge a vCenter discovery pass, preserving any existing selection."""
        summary = {"added": 0, "updated": 0}
        for vm in vms:
            d = vm.to_dict() if hasattr(vm, "to_dict") else dict(vm)
            nics = d.get("nics", [])
            networks = ",".join(
                n.get("network_name") or n.get("network_id") or "" for n in nics)
            existing = self.conn.execute(
                "SELECT moref FROM discovered WHERE moref=?", (d["moref"],)).fetchone()
            if existing:
                self.conn.execute(
                    "UPDATE discovered SET name=?, power_state=?, cpu_count=?, memory_mb=?,"
                    " folder=?, datacenter=?, cluster=?, host=?, guest_os=?, tools_status=?,"
                    " nics_json=?, networks=?, discovered_at=? WHERE moref=?",
                    (d.get("name", ""), d.get("power_state", ""), d.get("cpu_count", 0),
                     d.get("memory_mb", 0), d.get("folder", ""), d.get("datacenter", ""),
                     d.get("cluster", ""), d.get("host", ""), d.get("guest_os", ""),
                     d.get("tools_status", ""), json.dumps(nics), networks, _now(), d["moref"]),
                )
                summary["updated"] += 1
            else:
                self.conn.execute(
                    "INSERT INTO discovered(moref, name, power_state, cpu_count, memory_mb,"
                    " folder, datacenter, cluster, host, guest_os, tools_status, nics_json,"
                    " networks, discovered_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (d["moref"], d.get("name", ""), d.get("power_state", ""),
                     d.get("cpu_count", 0), d.get("memory_mb", 0), d.get("folder", ""),
                     d.get("datacenter", ""), d.get("cluster", ""), d.get("host", ""),
                     d.get("guest_os", ""), d.get("tools_status", ""), json.dumps(nics),
                     networks, _now()),
                )
                summary["added"] += 1
        self.conn.commit()
        return summary

    def query_discovered(
        self,
        selected: Optional[bool] = None,
        morefs: Optional[Sequence[str]] = None,
        limit: Optional[int] = None,
        order: str = "name COLLATE NOCASE",
    ) -> List[sqlite3.Row]:
        sql = "SELECT * FROM discovered WHERE 1=1"
        params: List[Any] = []
        if selected is not None:
            sql += " AND selected=?"
            params.append(1 if selected else 0)
        if morefs:
            sql += " AND moref IN ({})".format(",".join("?" * len(morefs)))
            params += list(morefs)
        sql += " ORDER BY " + order
        if limit:
            sql += " LIMIT {}".format(int(limit))
        return list(self.conn.execute(sql, params).fetchall())

    def set_selected(
        self,
        morefs: Sequence[str],
        selected: bool,
        namespace: Optional[str] = None,
        wave: Optional[int] = None,
    ) -> int:
        count = 0
        for moref in morefs:
            sets = ["selected=?"]
            params: List[Any] = [1 if selected else 0]
            if namespace is not None:
                sets.append("namespace=?")
                params.append(namespace)
            if wave is not None:
                sets.append("wave=?")
                params.append(wave)
            params.append(moref)
            cur = self.conn.execute(
                "UPDATE discovered SET {} WHERE moref=?".format(", ".join(sets)), params)
            count += cur.rowcount
        self.conn.commit()
        return count

    def clear_selection(self) -> int:
        cur = self.conn.execute("UPDATE discovered SET selected=0 WHERE selected=1")
        self.conn.commit()
        return cur.rowcount

    def discovered_counts(self) -> Dict[str, int]:
        row = self.conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(selected), 0) AS selected FROM discovered"
        ).fetchone()
        return {"total": row["total"], "selected": row["selected"]}

    def discovered_facets(self) -> Dict[str, List[str]]:
        """Distinct clusters, folders and networks, for filtering and the picker."""
        out: Dict[str, List[str]] = {}
        for field_name in ("datacenter", "cluster", "folder", "power_state"):
            rows = self.conn.execute(
                "SELECT DISTINCT {0} AS v FROM discovered WHERE {0} != '' ORDER BY v"
                .format(field_name)).fetchall()
            out[field_name] = [r["v"] for r in rows]
        nets = set()
        for row in self.conn.execute("SELECT networks FROM discovered").fetchall():
            for name in (row["networks"] or "").split(","):
                if name:
                    nets.add(name)
        out["network"] = sorted(nets)
        return out

    def folder_tree(self) -> List[Dict[str, Any]]:
        """Distinct folder paths with direct and subtree VM counts, sorted for display."""
        direct: Dict[Tuple[str, str], int] = {}
        selected: Dict[Tuple[str, str], int] = {}
        for row in self.conn.execute(
                "SELECT datacenter, folder, selected FROM discovered").fetchall():
            key = (row["datacenter"] or "", row["folder"] or "")
            direct[key] = direct.get(key, 0) + 1
            if row["selected"]:
                selected[key] = selected.get(key, 0) + 1
        # Make sure every ancestor appears even if it holds no VMs directly.
        keys = set(direct)
        for dc, path in list(direct):
            parts = path.split("/") if path else []
            for i in range(len(parts)):
                keys.add((dc, "/".join(parts[:i])))
        out: List[Dict[str, Any]] = []
        for dc, path in sorted(keys, key=lambda k: (k[0], k[1].lower())):
            prefix = path + "/" if path else ""
            subtree = sum(n for (d, p), n in direct.items()
                          if d == dc and (p == path or p.startswith(prefix)))
            sub_sel = sum(n for (d, p), n in selected.items()
                          if d == dc and (p == path or p.startswith(prefix)))
            out.append({
                "datacenter": dc, "path": path, "depth": path.count("/") + (1 if path else 0),
                "direct": direct.get((dc, path), 0), "subtree": subtree, "selected": sub_sel,
            })
        return out

    # ---------------------------------------------------------- events
    def log_event(
        self,
        message: str,
        level: str = "info",
        moref: Optional[str] = None,
        batch: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO events(ts, level, moref, batch, message) VALUES(?,?,?,?,?)",
            (_now(), level, moref, batch, message),
        )
        self.conn.commit()

    def recent_events(self, limit: int = 50, level: Optional[str] = None) -> List[sqlite3.Row]:
        sql = "SELECT * FROM events"
        params: List[Any] = []
        if level:
            sql += " WHERE level=?"
            params.append(level)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return list(self.conn.execute(sql, params).fetchall())
