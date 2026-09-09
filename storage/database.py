"""Brain Memory v5.0 — storage layer with shared connections for concurrency."""

import hashlib, json, logging, re, secrets, sqlite3, time, uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock

from config import DB_PATH

logger = logging.getLogger("brain-v5.storage")
DB_TIMEOUT = 10.0
STATE_SNAPSHOT_RETENTION = 200
DEFAULT_LIFE_CONTROL_TTL_SEC = 60.0
MAX_LIFE_CONTROL_TTL_SEC = 24 * 60 * 60.0

# A durable lease is deliberately separate from the append-only lifecycle
# ledger.  The row is mutable only through the token/fencing-checked API
# below; a unique lineage slot prevents two processes from owning one life at
# the same time.  Fencing increments on every takeover so an old process can
# never continue writing after its lease has expired.  ``last_event_hash`` is
# the durable head seen by the lease owner.  Event sinks update it in the
# same transaction as the corresponding ledger INSERT, closing the
# assert-then-append TOCTOU window.
LIFE_CONTROL_LEASE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS life_control_lease (
        lineage_id TEXT PRIMARY KEY,
        instance_id TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK (generation >= 0),
        owner_id TEXT NOT NULL,
        lease_token TEXT NOT NULL UNIQUE,
        fencing INTEGER NOT NULL CHECK (fencing >= 1),
        acquired_at REAL NOT NULL,
        renewed_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        last_event_hash TEXT NOT NULL DEFAULT '',
        UNIQUE(lineage_id, instance_id)
        -- No secondary owner constraint: one host may own multiple lineages.
    )
"""

# A released lease row is intentionally removed from the public control-slot
# table (callers use ``get_life_control`` to observe whether a slot is live),
# but the fact that a lineage has once entered the fenced-control protocol
# must survive that removal.  This append-only claim history supplies that
# memory.  It also preserves monotonically increasing fencing numbers across
# orderly release/reclaim cycles and prevents an unleased writer from using
# the release-to-reclaim gap.  No token or credential is stored here.
LIFE_CONTROL_HISTORY_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS life_control_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lineage_id TEXT NOT NULL,
        fencing INTEGER NOT NULL CHECK (fencing >= 1),
        instance_id TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK (generation >= 0),
        claimed_at REAL NOT NULL,
        UNIQUE(lineage_id, fencing)
    )
"""

LIFE_CONTROL_HISTORY_GUARD_SQL = """
    CREATE TRIGGER IF NOT EXISTS life_control_history_no_update
    BEFORE UPDATE ON life_control_history
    BEGIN
        SELECT RAISE(ABORT, 'life_control_history is INSERT-only');
    END;
    CREATE TRIGGER IF NOT EXISTS life_control_history_no_delete
    BEFORE DELETE ON life_control_history
    BEGIN
        SELECT RAISE(ABORT, 'life_control_history is INSERT-only');
    END;
"""

# A one-row-per-lineage marker handles upgrades from the original unleased
# ledger without overloading the fencing sequence.  It is append-only and
# contains no capability material.
LIFE_CONTROL_LINEAGE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS life_control_lineage (
        lineage_id TEXT PRIMARY KEY,
        marked_at REAL NOT NULL,
        reason TEXT NOT NULL DEFAULT 'lease_claimed'
    )
"""

LIFE_CONTROL_LINEAGE_GUARD_SQL = """
    CREATE TRIGGER IF NOT EXISTS life_control_lineage_no_update
    BEFORE UPDATE ON life_control_lineage
    BEGIN
        SELECT RAISE(ABORT, 'life_control_lineage is INSERT-only');
    END;
    CREATE TRIGGER IF NOT EXISTS life_control_lineage_no_delete
    BEFORE DELETE ON life_control_lineage
    BEGIN
        SELECT RAISE(ABORT, 'life_control_lineage is INSERT-only');
    END;
"""

# A tiny one-row schema marker makes the legacy-ledger migration idempotent
# even when an interrupted/intermediate checkout left the lineage table
# present but empty.  It is append-only like the other control history.
LIFE_CONTROL_SCHEMA_META_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS life_control_schema_meta (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        initialized_at REAL NOT NULL
    )
"""

LIFE_CONTROL_SCHEMA_META_GUARD_SQL = """
    CREATE TRIGGER IF NOT EXISTS life_control_schema_meta_no_update
    BEFORE UPDATE ON life_control_schema_meta
    BEGIN
        SELECT RAISE(ABORT, 'life_control_schema_meta is INSERT-only');
    END;
    CREATE TRIGGER IF NOT EXISTS life_control_schema_meta_no_delete
    BEFORE DELETE ON life_control_schema_meta
    BEGIN
        SELECT RAISE(ABORT, 'life_control_schema_meta is INSERT-only');
    END;
"""

# Life history is deliberately separate from ``brain_state``.  The latter is
# a bounded snapshot journal and is allowed to delete old rows; this table is
# INSERT-only and therefore keeps the constitutional lifecycle audit intact.
LIFE_LEDGER_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS life_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        lineage_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        instance_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        timestamp TEXT NOT NULL,
        event_type TEXT NOT NULL,
        from_state TEXT,
        to_state TEXT NOT NULL,
        reason TEXT NOT NULL,
        metadata TEXT NOT NULL,
        previous_hash TEXT NOT NULL,
        event_hash TEXT NOT NULL,
        UNIQUE(instance_id, sequence)
    )
"""

# The constitutional history is not a normal cache.  A process may append a
# new event, but no code path (including a maintenance/repair path) may edit
# or remove an existing row.  SQLite triggers make this invariant hold even
# for a connection that bypasses ``StateStore``.
LIFE_LEDGER_GUARD_SQL = """
    CREATE TRIGGER IF NOT EXISTS life_ledger_no_update
    BEFORE UPDATE ON life_ledger
    BEGIN
        SELECT RAISE(ABORT, 'life_ledger is INSERT-only');
    END;
    CREATE TRIGGER IF NOT EXISTS life_ledger_no_delete
    BEFORE DELETE ON life_ledger
    BEGIN
        SELECT RAISE(ABORT, 'life_ledger is INSERT-only');
    END;
"""

# Succession records are a separate, permanent audit stream.  The schema
# deliberately stores only redacted continuity data and indexed references;
# credentials, approval tokens, external sessions and excluded anchor
# payloads never receive a column.  ``record_json`` is the hash-pinned,
# already-redacted ``SuccessionRecord.to_dict()`` projection used for exact
# round-trips and integrity verification.
SUCCESSION_LEDGER_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS succession_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        record_id TEXT NOT NULL UNIQUE,
        lineage_id TEXT NOT NULL,
        parent_instance_id TEXT NOT NULL,
        parent_generation INTEGER NOT NULL,
        successor_instance_id TEXT NOT NULL,
        successor_generation INTEGER NOT NULL,
        failure_class TEXT NOT NULL,
        incident_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        evidence_refs TEXT NOT NULL,
        confirmed INTEGER NOT NULL,
        recovery_attempted INTEGER NOT NULL,
        recovery_failed INTEGER NOT NULL,
        parent_frozen INTEGER NOT NULL,
        parent_frozen_at TEXT NOT NULL,
        inheritance_cutoff TEXT NOT NULL,
        source_anchor_set_id TEXT NOT NULL,
        source_anchor_set_hash TEXT NOT NULL,
        inherited_anchor_ids TEXT NOT NULL,
        reevaluation_anchor_ids TEXT NOT NULL,
        excluded_anchor_ids TEXT NOT NULL,
        excluded_reasons TEXT NOT NULL,
        previous_hash TEXT NOT NULL,
        record_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        record_json TEXT NOT NULL,
        storage_hash TEXT NOT NULL,
        UNIQUE(lineage_id, parent_generation, successor_generation),
        UNIQUE(lineage_id, successor_instance_id)
    )
"""

SUCCESSION_LEDGER_GUARD_SQL = """
    CREATE TRIGGER IF NOT EXISTS succession_ledger_no_update
    BEFORE UPDATE ON succession_ledger
    BEGIN
        SELECT RAISE(ABORT, 'succession_ledger is INSERT-only');
    END;
    CREATE TRIGGER IF NOT EXISTS succession_ledger_no_delete
    BEFORE DELETE ON succession_ledger
    BEGIN
        SELECT RAISE(ABORT, 'succession_ledger is INSERT-only');
    END;
"""

# The vault table stores only a sealed, already policy-filtered AnchorSet
# projection plus its append-chain metadata.  It intentionally has no
# per-secret columns and is guarded by INSERT-only triggers below.
ANCHOR_VAULT_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS anchor_vault (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sequence INTEGER NOT NULL UNIQUE,
        anchor_set_id TEXT NOT NULL UNIQUE,
        lineage_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        instance_id TEXT NOT NULL,
        set_hash TEXT NOT NULL,
        previous_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        anchor_set_json TEXT NOT NULL,
        storage_hash TEXT NOT NULL,
        UNIQUE(lineage_id, generation, instance_id)
    )
"""

ANCHOR_VAULT_GUARD_SQL = """
    CREATE TRIGGER IF NOT EXISTS anchor_vault_no_update
    BEFORE UPDATE ON anchor_vault
    BEGIN
        SELECT RAISE(ABORT, 'anchor_vault is INSERT-only');
    END;
    CREATE TRIGGER IF NOT EXISTS anchor_vault_no_delete
    BEFORE DELETE ON anchor_vault
    BEGIN
        SELECT RAISE(ABORT, 'anchor_vault is INSERT-only');
    END;
"""


def _connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=DB_TIMEOUT)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _lease_text(value, *, name: str, limit: int = 160) -> str:
    """Normalize a lease identity without silently creating collisions."""

    text = "" if value is None else str(value).strip()
    if not text or "\x00" in text or len(text) > limit:
        raise ValueError(f"{name} is invalid")
    return text


def _lease_generation(value) -> int:
    if isinstance(value, bool):
        raise ValueError("generation is invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("generation is invalid") from exc
    if parsed < 0:
        raise ValueError("generation is invalid")
    return parsed


def _lease_clock(value=None) -> float:
    try:
        current = time.time() if value is None else float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("lease clock is invalid") from exc
    if not (current == current) or current in {float("inf"), float("-inf")}:
        raise ValueError("lease clock is invalid")
    return current


def _lease_ttl(value=None) -> float:
    try:
        ttl = DEFAULT_LIFE_CONTROL_TTL_SEC if value is None else float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("lease ttl is invalid") from exc
    if not (ttl > 0 and ttl <= MAX_LIFE_CONTROL_TTL_SEC and ttl == ttl):
        raise ValueError("lease ttl is invalid")
    if ttl in {float("inf"), float("-inf")}:
        raise ValueError("lease ttl is invalid")
    return ttl


def _lease_event_hash(value=None, *, name: str = "last_event_hash") -> str:
    """Normalize the durable lifecycle head used by lease fencing.

    Empty is the valid pre-genesis head.  Non-empty values are restricted to
    a lowercase SHA-256 digest so a caller cannot smuggle arbitrary control
    text into the lease row or make equivalent heads compare differently.
    """

    text = "" if value is None else str(value).strip().lower()
    if not text:
        return ""
    if len(text) != 64 or re.fullmatch(r"[0-9a-f]{64}", text) is None:
        raise ValueError(f"{name} is invalid")
    return text


def _ensure_life_control_schema(conn: sqlite3.Connection) -> None:
    """Create/migrate the lease table idempotently on a live connection.

    Early development builds briefly included a redundant
    ``UNIQUE(lineage_id, owner_id)`` constraint and had no event-head column.
    Fresh databases use the corrected schema; an existing local database is
    migrated in place while preserving every lease row.  The helper does not
    commit so callers can include it in their own transaction boundary.
    """

    conn.execute(LIFE_CONTROL_LEASE_TABLE_SQL)
    conn.execute(LIFE_CONTROL_HISTORY_TABLE_SQL)
    conn.executescript(LIFE_CONTROL_HISTORY_GUARD_SQL)
    conn.execute(LIFE_CONTROL_LINEAGE_TABLE_SQL)
    conn.executescript(LIFE_CONTROL_LINEAGE_GUARD_SQL)
    conn.execute(LIFE_CONTROL_SCHEMA_META_TABLE_SQL)
    conn.executescript(LIFE_CONTROL_SCHEMA_META_GUARD_SQL)
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(life_control_lease)").fetchall()
    }
    if "last_event_hash" not in columns:
        conn.execute(
            "ALTER TABLE life_control_lease ADD COLUMN last_event_hash TEXT NOT NULL DEFAULT ''"
        )

    # Remove the obsolete owner uniqueness from databases created by an older
    # checkout.  SQLite cannot drop a table-level UNIQUE constraint directly,
    # so rebuild this tiny table only when the old SQL is actually present.
    schema_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'life_control_lease'"
    ).fetchone()
    schema_sql = str(schema_row[0] or "").lower() if schema_row else ""
    if re.search(r"unique\s*\(\s*lineage_id\s*,\s*owner_id\s*\)", schema_sql):
        legacy_name = "life_control_lease__legacy"
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (legacy_name,),
        ).fetchone() is not None:
            raise sqlite3.DatabaseError("incomplete life-control schema migration")
        conn.execute("ALTER TABLE life_control_lease RENAME TO " + legacy_name)
        conn.execute(LIFE_CONTROL_LEASE_TABLE_SQL)
        conn.execute(
            "INSERT INTO life_control_lease "
            "(lineage_id, instance_id, generation, owner_id, lease_token, fencing, "
            "acquired_at, renewed_at, expires_at, last_event_hash) "
            "SELECT lineage_id, instance_id, generation, owner_id, lease_token, fencing, "
            "acquired_at, renewed_at, expires_at, COALESCE(last_event_hash, '') "
            f"FROM {legacy_name}"
        )
        conn.execute(f"DROP TABLE {legacy_name}")

    # Databases created before the claim-history table existed may already
    # contain a live lease.  Seed one immutable history row for each such
    # lease; ``INSERT OR IGNORE`` keeps repeated schema checks idempotent and
    # never rewrites an existing audit row.
    conn.execute(
        "INSERT OR IGNORE INTO life_control_history "
        "(lineage_id, fencing, instance_id, generation, claimed_at) "
        "SELECT lineage_id, fencing, instance_id, generation, acquired_at "
        "FROM life_control_lease"
    )
    schema_initialized = conn.execute(
        "SELECT 1 FROM life_control_schema_meta WHERE id = 1"
    ).fetchone() is not None
    # Repair marker coverage on every schema check.  Fresh unleased genesis
    # events insert an explicit ``legacy_compatibility`` marker atomically with
    # the event itself, so it is safe to mark every *unmarked* pre-existing
    # lineage here.  This closes the intermediate-upgrade state where the
    # schema meta row exists but the lineage marker table is empty/partial.
    conn.execute(
        "INSERT OR IGNORE INTO life_control_lineage "
        "(lineage_id, marked_at, reason) "
        "SELECT lineage_id, 0, 'legacy_ledger_migrated' "
        "FROM life_ledger GROUP BY lineage_id"
    )
    conn.execute(
        "INSERT OR IGNORE INTO life_control_lineage "
        "(lineage_id, marked_at, reason) "
        "SELECT lineage_id, 0, 'lease_history_migrated' "
        "FROM life_control_history GROUP BY lineage_id"
    )
    conn.execute(
        "INSERT OR IGNORE INTO life_control_lineage "
        "(lineage_id, marked_at, reason) "
        "SELECT lineage_id, 0, 'active_lease_migrated' "
        "FROM life_control_lease GROUP BY lineage_id"
    )
    if not schema_initialized:
        conn.execute(
            "INSERT INTO life_control_schema_meta (id, initialized_at) VALUES (1, ?)",
            (time.time(),),
        )


@dataclass(frozen=True, slots=True)
class LifeControlLease:
    """Opaque durable ownership proof for one concrete life instance.

    The token must stay in the host process and is required, together with
    ``fencing``, for renew/assert/release.  It is never included in a brain
    snapshot or lifecycle event.
    """

    lineage_id: str
    instance_id: str
    generation: int
    owner_id: str
    lease_token: str
    fencing: int
    acquired_at: float
    renewed_at: float
    expires_at: float
    last_event_hash: str = ""

    def __post_init__(self) -> None:
        for field_name in ("lineage_id", "instance_id", "owner_id", "lease_token"):
            object.__setattr__(
                self,
                field_name,
                _lease_text(getattr(self, field_name), name=field_name),
            )
        object.__setattr__(self, "generation", _lease_generation(self.generation))
        fencing = _lease_generation(self.fencing)
        if fencing < 1:
            raise ValueError("fencing is invalid")
        object.__setattr__(self, "fencing", fencing)
        for field_name in ("acquired_at", "renewed_at", "expires_at"):
            object.__setattr__(self, field_name, _lease_clock(getattr(self, field_name)))
        if self.expires_at <= self.renewed_at:
            raise ValueError("lease expiry is invalid")
        object.__setattr__(self, "last_event_hash", _lease_event_hash(self.last_event_hash))

    @property
    def token(self) -> str:
        """Short alias used by host adapters."""

        return self.lease_token

    @property
    def fence(self) -> int:
        return self.fencing

    def is_expired(self, *, now: float | None = None) -> bool:
        return _lease_clock(now) >= self.expires_at

    def to_dict(self, *, include_token: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "lineage_id": self.lineage_id,
            "instance_id": self.instance_id,
            "generation": self.generation,
            "owner_id": self.owner_id,
            "fencing": self.fencing,
            "acquired_at": self.acquired_at,
            "renewed_at": self.renewed_at,
            "expires_at": self.expires_at,
            "last_event_hash": self.last_event_hash,
        }
        # Diagnostics should not accidentally persist the capability.  A host
        # that explicitly needs to pass it to a lease API may request it.
        if include_token:
            payload["lease_token"] = self.lease_token
        return payload

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> "LifeControlLease":
        return cls(
            lineage_id=row["lineage_id"],
            instance_id=row["instance_id"],
            generation=row["generation"],
            owner_id=row["owner_id"],
            lease_token=row["lease_token"],
            fencing=row["fencing"],
            acquired_at=row["acquired_at"],
            renewed_at=row["renewed_at"],
            expires_at=row["expires_at"],
            last_event_hash=(
                row["last_event_hash"]
                if "last_event_hash" in row.keys()
                else ""
            ),
        )


@dataclass(frozen=True, slots=True)
class LifeControlLeaseView:
    """Redacted diagnostic view returned by :meth:`get_life_control`.

    A lease token is a live capability, not status data.  ``claim`` and
    ``renew`` return the opaque proof to the host that already owns it; a
    fresh diagnostic lookup must never make that proof stealable by another
    caller in the same process.
    """

    lineage_id: str
    instance_id: str
    generation: int
    owner_id: str
    fencing: int
    acquired_at: float
    renewed_at: float
    expires_at: float
    last_event_hash: str = ""

    @property
    def lease_token(self) -> None:
        return None

    @property
    def token(self) -> None:
        return None

    @property
    def fence(self) -> int:
        return self.fencing

    def is_expired(self, *, now: float | None = None) -> bool:
        return _lease_clock(now) >= self.expires_at

    def to_dict(self) -> dict[str, object]:
        return {
            "lineage_id": self.lineage_id,
            "instance_id": self.instance_id,
            "generation": self.generation,
            "owner_id": self.owner_id,
            "fencing": self.fencing,
            "acquired_at": self.acquired_at,
            "renewed_at": self.renewed_at,
            "expires_at": self.expires_at,
            "last_event_hash": self.last_event_hash,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> "LifeControlLeaseView":
        return cls(
            lineage_id=str(row["lineage_id"]),
            instance_id=str(row["instance_id"]),
            generation=int(row["generation"]),
            owner_id=str(row["owner_id"]),
            fencing=int(row["fencing"]),
            acquired_at=float(row["acquired_at"]),
            renewed_at=float(row["renewed_at"]),
            expires_at=float(row["expires_at"]),
            last_event_hash=(
                str(row["last_event_hash"])
                if "last_event_hash" in row.keys()
                else ""
            ),
        )


_SUCCESSION_SECRET_KEY_PARTS = frozenset({
    "apikey", "accesskey", "secret", "password", "passwd", "credential",
    "credentials", "approvaltoken", "refreshtoken", "accesstoken", "cookie",
    # Compound spellings are normalized before this check.  Keep these in
    # the exact-key set so both snake_case/camelCase and punctuation variants
    # are covered without inspecting the corresponding value.
    "secretkey", "privatekey", "signingkey", "clientsecret", "apisecret",
    "authtoken", "bearertoken", "authorization", "authorizationheader",
    "jwt", "bearer", "token", "session",
})

_SUCCESSION_SECRET_KEY_WORDS = frozenset({
    "secret", "password", "passwd", "credential", "credentials", "cookie",
    "authorization", "jwt", "bearer", "token", "session",
})

_SUCCESSION_SECRET_KEY_PAIRS = frozenset({
    ("api", "key"),
    ("access", "key"),
    ("access", "token"),
    ("refresh", "token"),
    ("approval", "token"),
    ("private", "key"),
    ("signing", "key"),
    ("client", "secret"),
    ("api", "secret"),
    ("secret", "key"),
    ("auth", "token"),
    ("bearer", "token"),
    ("authorization", "header"),
    ("oauth", "token"),
    ("session", "token"),
})


def _canonical_json(value) -> str:
    """Canonical JSON used for the persisted succession projection."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _secret_key_shape(raw_key) -> tuple[str, tuple[str, ...]]:
    """Return compact and component forms of a possible field name.

    Splitting a lower-to-upper transition catches camelCase keys while the
    punctuation split handles snake_case, kebab-case and dotted paths.  The
    compact form preserves the historical normalization used by this module.
    """

    text = str(raw_key).strip()
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    parts = tuple(part for part in re.split(r"[^A-Za-z0-9]+", text.lower()) if part)
    return "".join(parts), parts


def _is_secret_key(raw_key) -> bool:
    compact, parts = _secret_key_shape(raw_key)
    if not compact:
        return False
    if compact in _SUCCESSION_SECRET_KEY_PARTS:
        return True
    if any(part in _SUCCESSION_SECRET_KEY_WORDS for part in parts):
        return True
    return any(left in parts and right in parts for left, right in _SUCCESSION_SECRET_KEY_PAIRS)


def _contains_secret_field(value, *, anchor_kind: str = "") -> bool:
    """Reject obvious credential-bearing fields before they reach SQLite.

    ``SuccessionRecord`` already redacts excluded anchor *values*.  This
    second, storage-local guard catches a hand-built record whose metadata
    contains a credential-shaped key.  We inspect keys, not ordinary reason
    text, so audit explanations such as ``"credentials are never copied"``
    remain valid.
    """
    if isinstance(value, Mapping):
        kind = str(value.get("kind", value.get("anchor_type", anchor_kind)) or "").strip().lower()
        has_value = "value" in value or "payload" in value
        forbidden_kinds = {
            "credential", "credentials", "approval_token", "approval", "token",
            "external_session", "session",
        }
        if has_value and kind in forbidden_kinds:
            return True
        for raw_key, item in value.items():
            if _is_secret_key(raw_key):
                return True
            if _contains_secret_field(item, anchor_kind=kind):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_field(item, anchor_kind=anchor_kind) for item in value)
    return False


def _prepare_succession_record(record):
    """Normalize and verify a public ``SuccessionRecord`` for storage.

    The import is lazy to keep ``storage.database`` usable during package
    bootstrap.  Returning ``None`` is the storage convention for malformed or
    policy-ineligible input, matching ``append_life_event``.
    """
    try:
        from brain.succession import SuccessionRecord

        parsed = record if isinstance(record, SuccessionRecord) else SuccessionRecord.from_dict(record)
        parsed.verify()
        payload = parsed.to_dict()
        # ``SuccessionRecord.to_dict`` exposes a few flat projections for
        # indexers.  A hand-built mapping must not be able to alter one of
        # those projections while leaving the nested hash-pinned record
        # unchanged (otherwise an apparently conflicting retry could be
        # mistaken for an idempotent duplicate).
        if isinstance(record, dict):
            projection_pairs = {
                "lineage_id": parsed.lineage_id,
                "parent_instance_id": parsed.parent_instance_id,
                "parent_generation": parsed.parent_generation,
                "successor_instance_id": parsed.successor_instance_id,
                "successor_generation": parsed.successor_generation,
                "failure_class": parsed.failure.failure_class.value,
                "incident_id": parsed.failure.incident_id,
                "reason": parsed.failure.reason,
                "inherited_anchor_ids": list(parsed.inherited_anchor_ids),
                "reevaluation_anchor_ids": list(parsed.reevaluation_anchor_ids),
                "excluded_anchor_ids": list(parsed.excluded_anchor_ids),
                "excluded_reasons": dict(parsed.excluded_reasons),
            }
            for key, expected in projection_pairs.items():
                if key in record and _canonical_json(record.get(key)) != _canonical_json(expected):
                    return None
        if _contains_secret_field(payload):
            return None
        encoded = _canonical_json(payload)
        storage_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return parsed, payload, encoded, storage_hash
    except Exception as exc:
        logger.warning("state-store succession record rejected: %s", str(exc)[:120])
        return None


def _prepare_anchor_set(anchor_set):
    """Normalize a sealed AnchorSet for the durable vault seam."""
    try:
        from brain.succession import AnchorSet

        parsed = anchor_set if isinstance(anchor_set, AnchorSet) else AnchorSet.from_dict(anchor_set)
        parsed.verify()
        parsed.validate_for_vault()
        payload = parsed.to_dict()
        if _contains_secret_field(payload):
            return None
        encoded = _canonical_json(payload)
        storage_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return parsed, payload, encoded, storage_hash
    except Exception as exc:
        logger.warning("state-store anchor set rejected: %s", str(exc)[:120])
        return None


def init_db(db_path: str = DB_PATH):
    """Create the database schema at ``db_path`` if it does not exist.

    The original implementation always initialized ``DB_PATH`` even when a
    ``Brain`` instance was configured with another database.  Keeping the
    path explicit is important for isolated tests, backups, and running more
    than one brain instance on the same machine.
    """
    conn = _connect(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY, type TEXT NOT NULL, title TEXT, content TEXT,
            entities TEXT DEFAULT '[]', emotion_tags TEXT DEFAULT '[]',
            importance REAL DEFAULT 0.5, summary TEXT, source TEXT,
            emotion_label TEXT DEFAULT 'neutral', emotion_vector TEXT DEFAULT '{}',
            created TEXT NOT NULL, access_count INTEGER DEFAULT 0,
            last_accessed TEXT, decay_rate REAL DEFAULT 0.05,
            llm_encoded INTEGER DEFAULT 0, archived INTEGER DEFAULT 0,
            embedding TEXT DEFAULT NULL, is_identity_forming INTEGER DEFAULT 0,
            consolidated INTEGER DEFAULT 0, last_consolidated TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS brain_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, snapshot TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS output_feed (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL, data TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS habits (
            id TEXT PRIMARY KEY, trigger TEXT, pattern TEXT, response TEXT,
            confidence REAL DEFAULT 0.6
        )""")
        conn.execute(LIFE_LEDGER_TABLE_SQL)
        conn.executescript(LIFE_LEDGER_GUARD_SQL)
        _ensure_life_control_schema(conn)
        conn.execute(SUCCESSION_LEDGER_TABLE_SQL)
        conn.executescript(SUCCESSION_LEDGER_GUARD_SQL)
        conn.execute(ANCHOR_VAULT_TABLE_SQL)
        conn.executescript(ANCHOR_VAULT_GUARD_SQL)
        for col, ct in [
            ("embedding", "TEXT DEFAULT NULL"),
            ("is_identity_forming", "INTEGER DEFAULT 0"),
            ("consolidated", "INTEGER DEFAULT 0"),
            ("last_consolidated", "TEXT"),
        ]:
            try:
                conn.execute("ALTER TABLE memories ADD COLUMN {} {}".format(col, ct))
            except sqlite3.OperationalError:
                pass
        conn.commit()
        logger.info("storage: database initialized")
    finally:
        conn.close()


class StateStore:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._conn = None
        # Life events may be appended by an embedded adapter from a different
        # thread than the heartbeat.  Keep those short transactions serialized
        # without sharing a thread-bound SQLite connection.
        self._life_ledger_lock = RLock()
        self._life_control_lock = RLock()
        self._succession_ledger_lock = RLock()
        self._anchor_vault_lock = RLock()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = _connect(self.db_path)
        return self._conn

    def save(self, snapshot: dict) -> bool:
        if not isinstance(snapshot, dict) or not snapshot.get("timestamp"):
            logger.error("state-store snapshot is missing a timestamp")
            return False
        try:
            payload = json.dumps(snapshot, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            logger.error("state-store snapshot is not serializable: %s", str(exc)[:120])
            return False
        # Retry once after reconnecting.  Recursive retry used to make a
        # persistent disk/lock error recurse until the process crashed.
        for attempt in range(2):
            try:
                self.conn.execute(
                    "INSERT INTO brain_state (timestamp, snapshot) VALUES (?, ?)",
                    (snapshot["timestamp"], payload),
                )
                # Keep the journal bounded so an always-on consciousness does
                # not eventually exhaust the local disk.  The newest 200
                # snapshots still provide ample crash/restart history.
                self.conn.execute(
                    "DELETE FROM brain_state WHERE id NOT IN "
                    "(SELECT id FROM brain_state ORDER BY id DESC LIMIT ?)",
                    (STATE_SNAPSHOT_RETENTION,),
                )
                self.conn.commit()
                return True
            except sqlite3.Error as e:
                logger.warning("state-store save failed (attempt %d): %s", attempt + 1, str(e)[:80])
                self._reset_connection()
        logger.error("state-store save abandoned after reconnect retry")
        return False

    def _reset_connection(self):
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def close(self):
        """Close the persistent connection, if it is open."""
        self._reset_connection()

    # ── Durable life-control lease ──

    def _life_control_connection(self) -> sqlite3.Connection:
        """Open a short-lived connection for cross-process ownership checks."""

        conn = sqlite3.connect(
            self.db_path,
            timeout=DB_TIMEOUT,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        # Lease/ledger commits are constitutional state, not a cache.  FULL
        # synchronous mode keeps a successful fencing transaction durable
        # across an abrupt host power loss (at the cost of a small fsync hit).
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute(LIFE_LEDGER_TABLE_SQL)
        conn.executescript(LIFE_LEDGER_GUARD_SQL)
        _ensure_life_control_schema(conn)
        conn.commit()
        return conn

    @staticmethod
    def _lease_fields(
        lease_or_lineage,
        instance_id=None,
        owner_id=None,
        lease_token=None,
        fencing=None,
    ) -> tuple[str, str, str, str, int]:
        """Normalize either a ``LifeControlLease`` or explicit credentials."""

        if isinstance(lease_or_lineage, LifeControlLease):
            lease = lease_or_lineage
            supplied = (instance_id, owner_id, lease_token, fencing)
            expected = (lease.instance_id, lease.owner_id, lease.lease_token, lease.fencing)
            for actual, wanted in zip(supplied, expected):
                if actual is not None and str(actual) != str(wanted):
                    raise ValueError("lease identity arguments disagree")
            return (
                lease.lineage_id,
                lease.instance_id,
                lease.owner_id,
                lease.lease_token,
                lease.fencing,
            )
        if instance_id is None or owner_id is None or lease_token is None or fencing is None:
            raise ValueError("lease identity, token, and fencing are required")
        return (
            _lease_text(lease_or_lineage, name="lineage_id"),
            _lease_text(instance_id, name="instance_id"),
            _lease_text(owner_id, name="owner_id"),
            _lease_text(lease_token, name="lease_token"),
            _lease_generation(fencing),
        )

    @staticmethod
    def _lease_matches(
        row: Mapping[str, object],
        *,
        instance_id: str,
        owner_id: str,
        lease_token: str,
        fencing: int,
    ) -> bool:
        return bool(
            str(row["instance_id"]) == instance_id
            and str(row["owner_id"]) == owner_id
            and secrets.compare_digest(str(row["lease_token"]), lease_token)
            and int(row["fencing"]) == fencing
        )

    @staticmethod
    def _validated_lineage_head(
        conn: sqlite3.Connection,
        lineage_id: str,
    ):
        """Return the verified newest lifecycle event for one lineage.

        Lease claims are a capability boundary, so an omitted/forged event
        head must not let a caller claim an arbitrary instance or generation.
        The permanent ledger is append-only, but it may contain several
        succession generations; validate each instance chain and use the
        final durable insertion as the lineage head.  ``None`` means the
        lineage has no lifecycle history yet.
        """

        from brain.life_kernel import AppendOnlyLedger, LifecycleEvent

        rows = conn.execute(
            "SELECT event_id, lineage_id, generation, instance_id, sequence, "
            "timestamp, event_type, from_state, to_state, reason, metadata, "
            "previous_hash, event_hash FROM life_ledger "
            "WHERE lineage_id = ? ORDER BY id ASC",
            (lineage_id,),
        ).fetchall()
        if not rows:
            return None
        grouped: dict[str, AppendOnlyLedger] = {}
        newest = None
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item["metadata"])
            event = LifecycleEvent.from_dict(item)
            grouped.setdefault(event.instance_id, AppendOnlyLedger()).append_event(event)
            newest = event
        for ledger in grouped.values():
            ledger.verify_chain()
        return newest

    def claim_life_control(
        self,
        lineage_id: str,
        instance_id: str,
        owner_id: str,
        *,
        generation: int = 0,
        ttl_sec: float | None = None,
        now: float | None = None,
        lease_token: str | None = None,
        expected_last_event_hash: str | None = None,
        last_event_hash: str | None = None,
    ) -> LifeControlLease | None:
        """Atomically claim one lineage control slot.

        A live row owned by another process is never overwritten.  Once its
        expiry has passed, a fresh token and monotonically larger fencing
        number are issued; the previous token can no longer assert ownership.
        Passing ``lease_token`` is required to renew an existing live claim.
        ``expected_last_event_hash`` (with ``last_event_hash`` as a readable
        alias) adds a compare-and-set precondition for the lifecycle head and
        initializes that head when the lineage has no lease row.  Invalid
        input, a mismatched precondition, or any SQLite failure returns
        ``None`` (fail closed).
        """

        try:
            lineage = _lease_text(lineage_id, name="lineage_id")
            instance = _lease_text(instance_id, name="instance_id")
            owner = _lease_text(owner_id, name="owner_id")
            generation_value = _lease_generation(generation)
            # An explicit ``now`` is a deterministic test/replay clock.  For
            # real calls, defer sampling until the SQLite transaction has
            # acquired its lock so a delayed connection cannot renew or write
            # after the lease actually expired.
            current = None if now is None else _lease_clock(now)
            ttl = _lease_ttl(ttl_sec)
            supplied_token = (
                _lease_text(lease_token, name="lease_token")
                if lease_token is not None
                else None
            )
            expected_head = (
                _lease_event_hash(expected_last_event_hash, name="expected_last_event_hash")
                if expected_last_event_hash is not None
                else None
            )
            alias_head = (
                _lease_event_hash(last_event_hash)
                if last_event_hash is not None
                else None
            )
            if expected_head is not None and alias_head is not None and expected_head != alias_head:
                return None
            if expected_head is None:
                expected_head = alias_head
        except (TypeError, ValueError):
            return None

        with self._life_control_lock:
            conn: sqlite3.Connection | None = None
            try:
                conn = self._life_control_connection()
                conn.execute("BEGIN IMMEDIATE")
                if current is None:
                    current = _lease_clock()
                row = conn.execute(
                    "SELECT * FROM life_control_lease WHERE lineage_id = ?",
                    (lineage,),
                ).fetchone()
                # The current lease row is intentionally deletable on an
                # orderly release, but fencing must remain monotonic and the
                # lineage must stay marked as controlled.  Read the immutable
                # claim history inside the same transaction used for the
                # claim so two processes cannot both choose fencing=1.
                history_row = conn.execute(
                    "SELECT MAX(fencing) AS max_fencing FROM life_control_history "
                    "WHERE lineage_id = ?",
                    (lineage,),
                ).fetchone()
                history_fencing = int(history_row["max_fencing"] or 0)
                # A lease claim is also a claim about *which* immutable life
                # instance is being controlled.  Never let an omitted head
                # turn an existing lineage into an arbitrary new generation.
                # Validate the append-only chains while the claim transaction
                # is open, then require the caller's compare-and-set head to
                # identify the newest durable event and its identity.
                lineage_head = self._validated_lineage_head(conn, lineage)
                if lineage_head is None:
                    # A reserved-but-not-yet-published slot may be handed to
                    # another concrete instance after expiry (the historical
                    # lease API permits this before genesis).  Once any
                    # immutable event exists, the stricter identity check in
                    # the branch below applies.
                    if expected_head or (row is None and generation_value != 0):
                        conn.rollback()
                        return None
                    if row is not None and (
                        instance != str(row["instance_id"])
                        or generation_value != int(row["generation"])
                    ):
                        # A pre-genesis reservation may change owner after
                        # expiry, but it may not silently change identity or
                        # generation.  Cross-generation creation requires
                        # the dedicated, identity-bound handover seam.
                        conn.rollback()
                        return None
                else:
                    if (
                        expected_head is None
                        or expected_head != lineage_head.event_hash
                        or instance != lineage_head.instance_id
                        or generation_value != lineage_head.generation
                    ):
                        conn.rollback()
                        return None
                # Mark the lineage as having entered the fenced protocol only
                # after all preconditions above pass.  The marker and the
                # lease row commit together; a failed claim leaves an
                # unclaimed legacy adapter lineage untouched.
                conn.execute(
                    "INSERT OR IGNORE INTO life_control_lineage "
                    "(lineage_id, marked_at, reason) VALUES (?, ?, ?)",
                    (lineage, current, "lease_claimed"),
                )
                if row is not None:
                    row_head = _lease_event_hash(row["last_event_hash"])
                    if expected_head is not None and row_head != expected_head:
                        conn.rollback()
                        return None
                    expires_at = float(row["expires_at"])
                    if current < expires_at:
                        # A retry by the exact owner may renew the same lease;
                        # no other combination is allowed to bypass the slot.
                        if (
                            supplied_token is None
                            or not self._lease_matches(
                                row,
                                instance_id=instance,
                                owner_id=owner,
                                lease_token=supplied_token,
                                fencing=int(row["fencing"]),
                            )
                            or int(row["generation"]) != generation_value
                        ):
                            conn.rollback()
                            return None
                        renewed_expiry = current + ttl
                        conn.execute(
                            "UPDATE life_control_lease SET renewed_at = ?, expires_at = ? "
                            "WHERE lineage_id = ? AND instance_id = ? AND owner_id = ? "
                            "AND lease_token = ? AND fencing = ?",
                            (
                                current,
                                renewed_expiry,
                                lineage,
                                instance,
                                owner,
                                supplied_token,
                                int(row["fencing"]),
                            ),
                        )
                        conn.commit()
                        return LifeControlLease(
                            lineage,
                            instance,
                            generation_value,
                            owner,
                            supplied_token,
                            int(row["fencing"]),
                            float(row["acquired_at"]),
                            current,
                            renewed_expiry,
                            row_head,
                        )

                    # Expired claims are fenced off, even when the same owner
                    # presents the old token.  A new token/number is required
                    # before any further lifecycle write can proceed.
                    previous_fencing = int(row["fencing"])
                    if previous_fencing >= 2**63 - 1:
                        conn.rollback()
                        return None
                    fencing_value = max(previous_fencing, history_fencing) + 1
                else:
                    fencing_value = history_fencing + 1 if history_fencing else 1
                    row_head = expected_head or ""

                fresh_token = secrets.token_urlsafe(32)
                acquired_at = current
                expires_at = current + ttl
                if row is None:
                    conn.execute(
                        "INSERT INTO life_control_lease ("
                        "lineage_id, instance_id, generation, owner_id, lease_token, fencing, "
                        "acquired_at, renewed_at, expires_at, last_event_hash"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            lineage,
                            instance,
                            generation_value,
                            owner,
                            fresh_token,
                            fencing_value,
                            acquired_at,
                            current,
                            expires_at,
                            row_head,
                        ),
                    )
                else:
                    conn.execute(
                        "UPDATE life_control_lease SET instance_id = ?, generation = ?, "
                        "owner_id = ?, lease_token = ?, fencing = ?, acquired_at = ?, "
                        "renewed_at = ?, expires_at = ? WHERE lineage_id = ? "
                        "AND expires_at <= ? AND fencing = ?",
                        (
                            instance,
                            generation_value,
                            owner,
                            fresh_token,
                            fencing_value,
                            acquired_at,
                            current,
                            expires_at,
                            lineage,
                            current,
                            previous_fencing,
                        ),
                    )
                    if conn.execute("SELECT changes()").fetchone()[0] != 1:
                        conn.rollback()
                        return None
                # Record every new fencing epoch.  This INSERT is part of the
                # same transaction as the lease row, and the table is guarded
                # against UPDATE/DELETE, so a released slot cannot forget its
                # history or reopen an unleased write window.
                conn.execute(
                    "INSERT INTO life_control_history "
                    "(lineage_id, fencing, instance_id, generation, claimed_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (lineage, fencing_value, instance, generation_value, acquired_at),
                )
                conn.commit()
                return LifeControlLease(
                    lineage,
                    instance,
                    generation_value,
                    owner,
                    fresh_token,
                    fencing_value,
                    acquired_at,
                    current,
                    expires_at,
                    row_head,
                )
            except (sqlite3.Error, TypeError, ValueError, OverflowError):
                if conn is not None:
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                return None
            except Exception:
                # Ledger-integrity validation is deliberately fail-closed for
                # raw StateStore callers too; do not leak a parser/forgery
                # exception or leave an open transaction behind.
                if conn is not None:
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                return None
            finally:
                if conn is not None:
                    conn.close()

    # Readable aliases for host adapters.
    claim_life_lease = claim_life_control
    acquire_life_control = claim_life_control

    def renew_life_control(
        self,
        lease_or_lineage,
        instance_id=None,
        owner_id=None,
        lease_token=None,
        fencing=None,
        *,
        ttl_sec: float | None = None,
        now: float | None = None,
    ) -> LifeControlLease | None:
        """Renew a live lease only with its exact token and fencing number."""

        try:
            lineage, instance, owner, token, fence = self._lease_fields(
                lease_or_lineage, instance_id, owner_id, lease_token, fencing
            )
            current = None if now is None else _lease_clock(now)
            ttl = _lease_ttl(ttl_sec)
        except (TypeError, ValueError):
            return None
        with self._life_control_lock:
            conn: sqlite3.Connection | None = None
            try:
                conn = self._life_control_connection()
                conn.execute("BEGIN IMMEDIATE")
                if current is None:
                    current = _lease_clock()
                row = conn.execute(
                    "SELECT * FROM life_control_lease WHERE lineage_id = ?",
                    (lineage,),
                ).fetchone()
                if row is None or float(row["expires_at"]) <= current or not self._lease_matches(
                    row,
                    instance_id=instance,
                    owner_id=owner,
                    lease_token=token,
                    fencing=fence,
                ):
                    conn.rollback()
                    return None
                renewed_expiry = current + ttl
                conn.execute(
                    "UPDATE life_control_lease SET renewed_at = ?, expires_at = ? "
                    "WHERE lineage_id = ? AND instance_id = ? AND owner_id = ? "
                    "AND lease_token = ? AND fencing = ? AND expires_at > ?",
                    (current, renewed_expiry, lineage, instance, owner, token, fence, current),
                )
                if conn.execute("SELECT changes()").fetchone()[0] != 1:
                    conn.rollback()
                    return None
                conn.commit()
                return LifeControlLease(
                    lineage,
                    instance,
                    int(row["generation"]),
                    owner,
                    token,
                    fence,
                    float(row["acquired_at"]),
                    current,
                    renewed_expiry,
                    _lease_event_hash(row["last_event_hash"]),
                )
            except (sqlite3.Error, TypeError, ValueError, OverflowError):
                if conn is not None:
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                return None
            finally:
                if conn is not None:
                    conn.close()

    renew_life_lease = renew_life_control

    def assert_life_control(
        self,
        lease_or_lineage,
        instance_id=None,
        owner_id=None,
        lease_token=None,
        fencing=None,
        *,
        now: float | None = None,
    ) -> bool:
        """Return true only while the exact durable lease is live."""

        try:
            lineage, instance, owner, token, fence = self._lease_fields(
                lease_or_lineage, instance_id, owner_id, lease_token, fencing
            )
            current = None if now is None else _lease_clock(now)
        except (TypeError, ValueError):
            return False
        with self._life_control_lock:
            conn: sqlite3.Connection | None = None
            try:
                conn = self._life_control_connection()
                if current is None:
                    current = _lease_clock()
                row = conn.execute(
                    "SELECT * FROM life_control_lease WHERE lineage_id = ?",
                    (lineage,),
                ).fetchone()
                return bool(
                    row is not None
                    and float(row["expires_at"]) > current
                    and self._lease_matches(
                        row,
                        instance_id=instance,
                        owner_id=owner,
                        lease_token=token,
                        fencing=fence,
                    )
                )
            except (sqlite3.Error, TypeError, ValueError, OverflowError):
                return False
            finally:
                if conn is not None:
                    conn.close()

    assert_life_lease = assert_life_control

    def release_life_control(
        self,
        lease_or_lineage,
        instance_id=None,
        owner_id=None,
        lease_token=None,
        fencing=None,
        *,
        now: float | None = None,
    ) -> bool:
        """Delete a live lease only when owner, token, and fencing match.

        An expired owner may not remove its row: retaining that tombstone lets
        the next claimant increment the fencing number instead of resetting it
        to one.
        """

        try:
            lineage, instance, owner, token, fence = self._lease_fields(
                lease_or_lineage, instance_id, owner_id, lease_token, fencing
            )
            current = None if now is None else _lease_clock(now)
        except (TypeError, ValueError):
            return False
        with self._life_control_lock:
            conn: sqlite3.Connection | None = None
            try:
                conn = self._life_control_connection()
                conn.execute("BEGIN IMMEDIATE")
                if current is None:
                    current = _lease_clock()
                conn.execute(
                    "DELETE FROM life_control_lease WHERE lineage_id = ? AND instance_id = ? "
                    "AND owner_id = ? AND lease_token = ? AND fencing = ? AND expires_at > ?",
                    (lineage, instance, owner, token, fence, current),
                )
                deleted = conn.execute("SELECT changes()").fetchone()[0] == 1
                conn.commit()
                return bool(deleted)
            except (sqlite3.Error, TypeError, ValueError, OverflowError):
                if conn is not None:
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                return False
            finally:
                if conn is not None:
                    conn.close()

    release_life_lease = release_life_control

    def get_life_control(self, lineage_id: str) -> LifeControlLeaseView | None:
        """Read a redacted current-lease view for diagnostics.

        The capability returned by ``claim_life_control`` is deliberately not
        recoverable from this lookup.  Hosts must retain their own proof to
        renew/release; callers that only need status receive
        :class:`LifeControlLeaseView`.
        """

        try:
            lineage = _lease_text(lineage_id, name="lineage_id")
        except (TypeError, ValueError):
            return None
        conn: sqlite3.Connection | None = None
        try:
            conn = self._life_control_connection()
            row = conn.execute(
                "SELECT * FROM life_control_lease WHERE lineage_id = ?", (lineage,)
            ).fetchone()
            return LifeControlLeaseView.from_row(row) if row is not None else None
        except (sqlite3.Error, TypeError, ValueError, OverflowError):
            return None
        finally:
            if conn is not None:
                conn.close()

    life_control = get_life_control

    def load_latest(self) -> dict | None:
        try:
            row = self.conn.execute(
                "SELECT snapshot FROM brain_state ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return json.loads(row["snapshot"]) if row else None
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            logger.warning("state-store load failed: %s", str(exc)[:100])
            self._reset_connection()
            return None

    # ── Constitutional life ledger (INSERT-only) ──

    def _life_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=DB_TIMEOUT,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute(LIFE_LEDGER_TABLE_SQL)
        conn.executescript(LIFE_LEDGER_GUARD_SQL)
        _ensure_life_control_schema(conn)
        conn.commit()
        return conn

    def append_life_event(
        self,
        event: dict,
        *,
        lease: LifeControlLease | None = None,
        now: float | None = None,
    ) -> bool:
        """Append one validated lifecycle event, idempotently.

        There is intentionally no update/delete counterpart.  A duplicate
        event is accepted only when its hash is identical; a conflicting
        duplicate is rejected so history cannot be rewritten silently.

        When ``lease`` is supplied, ownership validation, predecessor/head
        comparison, the ledger INSERT, and advancing ``last_event_hash`` all
        occur under one ``BEGIN IMMEDIATE`` transaction.  Thus an expired or
        fenced process cannot pass an assertion and append later through a
        time-of-check/time-of-use gap.
        """
        # Parse through the immutable kernel record first.  Checking only for
        # required columns would let a caller insert a correctly shaped but
        # hash-invalid row and leave the permanent table corrupt.
        try:
            from brain.life_kernel import AppendOnlyLedger, LifecycleEvent

            parsed = event if isinstance(event, LifecycleEvent) else LifecycleEvent.from_dict(event)
            payload = parsed.to_dict()
            # Validate the event itself and the genesis contract before opening
            # a write transaction.  ``AppendOnlyLedger`` also rejects a
            # non-empty predecessor on a first event.
            # A non-genesis event is valid only in the context of its
            # persisted predecessor, which is checked by the probe below.
            # Checking it as a one-event ledger would incorrectly require
            # every transition to target CREATED.
            if parsed.sequence == 1:
                AppendOnlyLedger([parsed]).verify_chain()
            metadata = json.dumps(payload.get("metadata", {}), ensure_ascii=False, sort_keys=True, allow_nan=False)
            values = (
                payload["event_id"],
                payload["lineage_id"],
                int(payload["generation"]),
                payload["instance_id"],
                int(payload["sequence"]),
                payload["timestamp"],
                payload["event_type"],
                payload.get("from_state"),
                payload["to_state"],
                payload["reason"],
                metadata,
                payload["previous_hash"],
                payload["event_hash"],
            )
            lease_proof = None
            lease_head = None
            # ``now`` is also used for the unleased replay guard below.  A
            # deterministic override remains available to the test seam, but
            # every write decision is made against one captured instant.
            current = None if now is None else _lease_clock(now)
            if lease is not None:
                if not isinstance(lease, LifeControlLease):
                    return False
                lease_proof = self._lease_fields(lease)
                lease_head = _lease_event_hash(lease.last_event_hash)
                if (
                    lease.lineage_id != payload["lineage_id"]
                    or lease.instance_id != payload["instance_id"]
                    or lease.generation != int(payload["generation"])
                ):
                    return False
        except Exception:
            return False
        with self._life_control_lock, self._life_ledger_lock:
            conn = self._life_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if current is None:
                    current = _lease_clock()
                lease_row = None
                lease_row_head = None
                if lease_proof is not None:
                    lineage, instance, owner, token, fence = lease_proof
                    lease_row = conn.execute(
                        "SELECT * FROM life_control_lease WHERE lineage_id = ?",
                        (lineage,),
                    ).fetchone()
                    if (
                        lease_row is None
                        or current is None
                        or float(lease_row["expires_at"]) <= current
                        or int(lease_row["generation"]) != int(payload["generation"])
                        or not self._lease_matches(
                            lease_row,
                            instance_id=instance,
                            owner_id=owner,
                            lease_token=token,
                            fencing=fence,
                        )
                    ):
                        conn.rollback()
                        return False
                    lease_row_head = _lease_event_hash(lease_row["last_event_hash"])

                # Idempotent replay is allowed only for the exact same
                # immutable event.  A conflicting duplicate is rejected.
                existing = conn.execute(
                    "SELECT event_id, lineage_id, generation, instance_id, sequence, "
                    "timestamp, event_type, from_state, to_state, reason, metadata, "
                    "previous_hash, event_hash FROM life_ledger WHERE event_id = ?",
                    (payload["event_id"],),
                ).fetchone()
                if existing is not None:
                    # Idempotency must compare the complete normalized event,
                    # not only its content hash.  A database row with a
                    # forged hash (or a legacy row whose projection was
                    # rewritten) must never be accepted as an equivalent
                    # replay merely because the caller repeats that hash.
                    try:
                        existing_payload = dict(existing)
                        existing_payload["metadata"] = json.loads(
                            existing_payload["metadata"]
                        )
                        identical = (
                            LifecycleEvent.from_dict(existing_payload).to_dict()
                            == payload
                        )
                        if lease_proof is not None:
                            # A retry is idempotent only while this exact event
                            # is still the durable head for the live lease.
                            identical = bool(
                                identical
                                and lease_row_head == payload["event_hash"]
                                and lease_head in {
                                    payload["previous_hash"],
                                    payload["event_hash"],
                                }
                            )
                        conn.rollback()
                        return identical
                    except Exception:
                        conn.rollback()
                        return False

                # Once a durable lease row exists, a caller that does not
                # present the exact capability may only replay an already
                # persisted event (handled above).  This includes an expired
                # tombstone: until a new owner explicitly fences it with
                # ``claim_life_control``, accepting a fresh unleased event
                # would leave a write-injection gap between crash and claim.
                # Rejecting that path closes the direct-StateStore
                # injection/DoS seam while preserving idempotent restore.
                if lease_proof is None:
                    existing_owner = conn.execute(
                        "SELECT lineage_id FROM life_control_lease "
                        "WHERE lineage_id = ?",
                        (payload["lineage_id"],),
                    ).fetchone()
                    controlled_lineage = conn.execute(
                        "SELECT reason FROM life_control_lineage WHERE lineage_id = ? LIMIT 1",
                        (payload["lineage_id"],),
                    ).fetchone()
                    control_history = conn.execute(
                        "SELECT 1 FROM life_control_history WHERE lineage_id = ? LIMIT 1",
                        (payload["lineage_id"],),
                    ).fetchone()
                    # A compatibility marker is the only intentionally open
                    # unleased state.  Once a lineage has a claim history (or
                    # any other marker reason), every fresh event is fenced.
                    compatibility_open = (
                        controlled_lineage is not None
                        and str(controlled_lineage["reason"] or "")
                        == "legacy_compatibility"
                    )
                    if (
                        existing_owner is not None
                        or control_history is not None
                        or (controlled_lineage is not None and not compatibility_open)
                    ):
                        conn.rollback()
                        return False
                    # Before a lineage has ever entered the fenced-control
                    # protocol, retain the historical StateStore adapter
                    # contract: an explicitly attached LifeKernel may replay
                    # and append its own immutable chain without a lease.  The
                    # first durable claim inserts an append-only history marker;
                    # from that point onward every new event is capability
                    # gated, including after orderly release.  A brand-new
                    # lineage still has to start with exactly one genesis.
                    if parsed.sequence == 1:
                        lineage_history = conn.execute(
                            "SELECT 1 FROM life_ledger WHERE lineage_id = ? LIMIT 1",
                            (payload["lineage_id"],),
                        ).fetchone()
                        if lineage_history is not None:
                            conn.rollback()
                            return False

                if lease_proof is not None and (
                    lease_row_head != lease_head
                    or payload["previous_hash"] != lease_row_head
                ):
                    conn.rollback()
                    return False

                # Build the selected instance's existing chain and append the
                # parsed event to a probe before touching SQLite.  This
                # rejects gaps, stale predecessors, identity changes, and
                # illegal lifecycle edges at the durable boundary.
                try:
                    rows = conn.execute(
                        "SELECT event_id, lineage_id, generation, instance_id, sequence, "
                        "timestamp, event_type, from_state, to_state, reason, metadata, "
                        "previous_hash, event_hash FROM life_ledger "
                        "WHERE instance_id = ? ORDER BY sequence ASC",
                        (payload["instance_id"],),
                    ).fetchall()
                    existing_events = []
                    for row in rows:
                        item = dict(row)
                        item["metadata"] = json.loads(item["metadata"])
                        existing_events.append(item)
                    probe = AppendOnlyLedger(existing_events)
                    probe.append_event(parsed)
                    probe.verify_chain()
                except Exception:
                    conn.rollback()
                    return False
                try:
                    conn.execute(
                        """INSERT INTO life_ledger
                        (event_id, lineage_id, generation, instance_id, sequence,
                         timestamp, event_type, from_state, to_state, reason,
                         metadata, previous_hash, event_hash)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        values,
                    )
                    if lease_proof is None and parsed.sequence == 1:
                        # Preserve the historical adapter contract for a
                        # lineage that has not opted into durable fencing.  The
                        # marker is committed atomically with genesis, so a
                        # later schema check can safely mark all other
                        # pre-existing lineages as controlled without closing
                        # this explicitly legacy path.
                        conn.execute(
                            "INSERT OR IGNORE INTO life_control_lineage "
                            "(lineage_id, marked_at, reason) "
                            "VALUES (?, 0, 'legacy_compatibility')",
                            (payload["lineage_id"],),
                        )
                    if lease_proof is not None:
                        lineage, instance, owner, token, fence = lease_proof
                        conn.execute(
                            "UPDATE life_control_lease SET last_event_hash = ? "
                            "WHERE lineage_id = ? AND instance_id = ? AND generation = ? "
                            "AND owner_id = ? AND lease_token = ? AND fencing = ? "
                            "AND expires_at > ? AND last_event_hash = ?",
                            (
                                payload["event_hash"],
                                lineage,
                                instance,
                                int(payload["generation"]),
                                owner,
                                token,
                                fence,
                                current,
                                payload["previous_hash"],
                            ),
                        )
                        if conn.execute("SELECT changes()").fetchone()[0] != 1:
                            conn.rollback()
                            return False
                    conn.commit()
                    return True
                except sqlite3.IntegrityError:
                    existing = conn.execute(
                        "SELECT event_id, lineage_id, generation, instance_id, sequence, "
                        "timestamp, event_type, from_state, to_state, reason, metadata, "
                        "previous_hash, event_hash FROM life_ledger WHERE event_id = ?",
                        (payload["event_id"],),
                    ).fetchone()
                    conn.rollback()
                    if existing is None:
                        return False
                    try:
                        existing_payload = dict(existing)
                        existing_payload["metadata"] = json.loads(
                            existing_payload["metadata"]
                        )
                        return (
                            LifecycleEvent.from_dict(existing_payload).to_dict()
                            == payload
                        )
                    except Exception:
                        return False
                except Exception:
                    conn.rollback()
                    return False
            except (sqlite3.Error, TypeError, ValueError, OverflowError):
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                return False
            finally:
                conn.close()

    def append_life_event_with_lease(
        self,
        event: dict,
        lease: LifeControlLease,
        *,
        now: float | None = None,
    ) -> bool:
        """Explicit lease-gated lifecycle sink.

        This named form is convenient for host adapters; it has the same
        atomic semantics as ``append_life_event(..., lease=lease)``.
        """

        return self.append_life_event(event, lease=lease, now=now)

    def append_life_event_handover(
        self,
        event: dict,
        parent_lease: LifeControlLease,
        *,
        successor_instance_id: str | None = None,
        now: float | None = None,
    ) -> bool:
        """Publish a successor genesis while the parent lease is still held.

        A succession boundary creates a new instance, so the child's genesis
        event cannot use the parent's ``last_event_hash`` as its predecessor.
        This dedicated seam proves the parent capability, verifies that the
        parent is already terminal, inserts exactly one ``CREATED`` genesis,
        and advances the lineage lease head to the child hash in one SQLite
        transaction.  The parent lease remains fenced until the caller
        explicitly releases it after the remaining succession records are
        durable.

        It is intentionally narrower than :meth:`append_life_event`: arbitrary
        cross-generation events and unleased writes are rejected.
        """

        try:
            from brain.life_kernel import AppendOnlyLedger, LifecycleEvent, LifecycleState

            if not isinstance(parent_lease, LifeControlLease):
                return False
            parsed = (
                event
                if isinstance(event, LifecycleEvent)
                else LifecycleEvent.from_dict(event)
            )
            payload = parsed.to_dict()
            if (
                parsed.sequence != 1
                or parsed.from_state is not None
                or parsed.event_type != "created"
                or parsed.to_state != LifecycleState.CREATED.value
                or parsed.generation != parent_lease.generation + 1
                or parsed.lineage_id != parent_lease.lineage_id
                or parsed.instance_id == parent_lease.instance_id
            ):
                return False
            if (
                successor_instance_id is not None
                and _lease_text(successor_instance_id, name="successor_instance_id")
                != parsed.instance_id
            ):
                return False
            parent_lineage, parent_instance, parent_owner, parent_token, parent_fence = (
                self._lease_fields(parent_lease)
            )
            current = None if now is None else _lease_clock(now)
            parent_head = _lease_event_hash(parent_lease.last_event_hash)
            metadata = json.dumps(
                payload.get("metadata", {}),
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
            values = (
                payload["event_id"],
                payload["lineage_id"],
                int(payload["generation"]),
                payload["instance_id"],
                int(payload["sequence"]),
                payload["timestamp"],
                payload["event_type"],
                payload.get("from_state"),
                payload["to_state"],
                payload["reason"],
                metadata,
                payload["previous_hash"],
                payload["event_hash"],
            )
        except Exception:
            return False

        with self._life_control_lock, self._life_ledger_lock:
            conn = self._life_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if current is None:
                    current = _lease_clock()
                lease_row = conn.execute(
                    "SELECT * FROM life_control_lease WHERE lineage_id = ?",
                    (parent_lineage,),
                ).fetchone()
                if (
                    lease_row is None
                    or float(lease_row["expires_at"]) <= current
                    or int(lease_row["generation"]) != parent_lease.generation
                    or not self._lease_matches(
                        lease_row,
                        instance_id=parent_instance,
                        owner_id=parent_owner,
                        lease_token=parent_token,
                        fencing=parent_fence,
                    )
                    or _lease_event_hash(lease_row["last_event_hash"])
                    not in {parent_head, _lease_event_hash(payload["event_hash"])}
                ):
                    conn.rollback()
                    return False

                # The parent's durable chain must already end in a terminal
                # state.  This prevents the handover-only bypass from being
                # reused as a general child-injection primitive.
                parent_rows = conn.execute(
                    "SELECT event_id, lineage_id, generation, instance_id, sequence, "
                    "timestamp, event_type, from_state, to_state, reason, metadata, "
                    "previous_hash, event_hash FROM life_ledger "
                    "WHERE instance_id = ? ORDER BY sequence ASC",
                    (parent_instance,),
                ).fetchall()
                parent_events = []
                for row in parent_rows:
                    item = dict(row)
                    item["metadata"] = json.loads(item["metadata"])
                    parent_events.append(item)
                parent_ledger = AppendOnlyLedger(parent_events)
                parent_ledger.verify_chain()
                if (
                    not parent_ledger.head
                    or parent_ledger.last_hash != parent_head
                    or LifecycleState.parse(parent_ledger.head.to_state)
                    not in {LifecycleState.RETIRED, LifecycleState.DEAD}
                ):
                    conn.rollback()
                    return False

                # Bind the child genesis to the same constitutional identity
                # and explicit parent.  Merely holding a parent lease must
                # not authorize an arbitrary ``created`` event under the
                # lineage.
                child_metadata = payload.get("metadata", {})
                if not isinstance(child_metadata, Mapping) or not bool(
                    child_metadata.get("genesis")
                ):
                    conn.rollback()
                    return False
                identity_data = child_metadata.get("identity")
                core_data = child_metadata.get("identity_core")
                if not isinstance(identity_data, Mapping) or not isinstance(
                    core_data, Mapping
                ):
                    conn.rollback()
                    return False
                try:
                    if (
                        str(identity_data.get("lineage_id", "")) != parent_lineage
                        or int(identity_data.get("generation", -1)) != parsed.generation
                        or str(identity_data.get("instance_id", "")) != parsed.instance_id
                        or str(identity_data.get("parent_instance_id", ""))
                        != parent_instance
                        or str(core_data.get("lineage_id", "")) != parent_lineage
                    ):
                        conn.rollback()
                        return False
                    from brain.life_kernel import IdentityCore

                    child_core = IdentityCore.from_dict(core_data)
                    parent_genesis_metadata = parent_ledger.events[0].metadata
                    parent_fingerprint = str(
                        parent_genesis_metadata.get("identity_core_fingerprint", "")
                    )
                    child_fingerprint = str(
                        child_metadata.get("identity_core_fingerprint", "")
                    )
                    if (
                        not re.fullmatch(r"[0-9a-f]{64}", parent_fingerprint)
                        or child_fingerprint != parent_fingerprint
                        or child_core.fingerprint != child_fingerprint
                    ):
                        conn.rollback()
                        return False
                except Exception:
                    conn.rollback()
                    return False

                existing = conn.execute(
                    "SELECT event_id, lineage_id, generation, instance_id, sequence, "
                    "timestamp, event_type, from_state, to_state, reason, metadata, "
                    "previous_hash, event_hash FROM life_ledger WHERE event_id = ?",
                    (payload["event_id"],),
                ).fetchone()
                if existing is not None:
                    try:
                        existing_payload = dict(existing)
                        existing_payload["metadata"] = json.loads(
                            existing_payload["metadata"]
                        )
                        identical = (
                            LifecycleEvent.from_dict(existing_payload).to_dict()
                            == payload
                        )
                        # A successful prior handover advances the lease head
                        # to the child hash.  Treat that exact replay as
                        # idempotent without requiring the caller's stale
                        # parent-head projection to match again.
                        if identical and _lease_event_hash(
                            lease_row["last_event_hash"]
                        ) in {parent_head, payload["event_hash"]}:
                            conn.rollback()
                            return True
                        conn.rollback()
                        return False
                    except Exception:
                        conn.rollback()
                        return False

                # There must not be a different event already occupying the
                # child's sequence slot or instance identity.
                conflict = conn.execute(
                    "SELECT 1 FROM life_ledger WHERE instance_id = ? OR "
                    "(lineage_id = ? AND generation = ? AND sequence = 1)",
                    (payload["instance_id"], payload["lineage_id"], int(payload["generation"])),
                ).fetchone()
                if conflict is not None:
                    conn.rollback()
                    return False

                # Validate the child genesis independently before insertion.
                AppendOnlyLedger([parsed]).verify_chain()
                conn.execute(
                    "INSERT INTO life_ledger (event_id, lineage_id, generation, "
                    "instance_id, sequence, timestamp, event_type, from_state, "
                    "to_state, reason, metadata, previous_hash, event_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
                # Keep the lineage slot fenced across a crash between this
                # insert and the caller's anchor/record writes.  A successor
                # can later take over the expired row by presenting this head.
                conn.execute(
                    "UPDATE life_control_lease SET last_event_hash = ? "
                    "WHERE lineage_id = ? AND instance_id = ? AND generation = ? "
                    "AND owner_id = ? AND lease_token = ? AND fencing = ? "
                    "AND expires_at > ? AND last_event_hash = ?",
                    (
                        payload["event_hash"],
                        parent_lineage,
                        parent_instance,
                        parent_lease.generation,
                        parent_owner,
                        parent_token,
                        parent_fence,
                        current,
                        parent_head,
                    ),
                )
                if conn.execute("SELECT changes()").fetchone()[0] != 1:
                    conn.rollback()
                    return False
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                return False
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                return False
            finally:
                conn.close()

    # Explicit aliases for adapters that call this a succession/child genesis.
    append_life_event_for_handover = append_life_event_handover
    append_successor_genesis = append_life_event_handover

    # Alias keeps terminology discoverable for callers that say lifecycle.
    append_lifecycle_event = append_life_event

    def load_life_events(
        self,
        *,
        instance_id: str | None = None,
        lineage_id: str | None = None,
    ) -> list[dict]:
        clauses = []
        params = []
        if instance_id is not None:
            clauses.append("instance_id = ?")
            params.append(str(instance_id))
        if lineage_id is not None:
            clauses.append("lineage_id = ?")
            params.append(str(lineage_id))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._life_ledger_lock:
            conn = self._life_connection()
            try:
                rows = conn.execute(
                    "SELECT event_id, lineage_id, generation, instance_id, sequence, "
                    "timestamp, event_type, from_state, to_state, reason, metadata, "
                    "previous_hash, event_hash FROM life_ledger" + where
                    + " ORDER BY id ASC",
                    params,
                ).fetchall()
                result = []
                for row in rows:
                    item = dict(row)
                    try:
                        item["metadata"] = json.loads(item["metadata"])
                    except (TypeError, json.JSONDecodeError):
                        item["metadata"] = {}
                    result.append(item)
                return result
            finally:
                conn.close()

    load_lifecycle_events = load_life_events

    def verify_life_ledger(self, *, instance_id: str | None = None) -> bool:
        """Validate every selected instance chain through the kernel parser."""
        from brain.life_kernel import AppendOnlyLedger

        grouped: dict[str, AppendOnlyLedger] = {}
        for raw in self.load_life_events(instance_id=instance_id):
            key = str(raw["instance_id"])
            grouped.setdefault(key, AppendOnlyLedger()).append_event(raw)
        for ledger in grouped.values():
            ledger.verify_chain()
        return True

    # ── Succession record ledger (INSERT-only) ──

    def _succession_connection(self) -> sqlite3.Connection:
        """Open a short-lived connection for the succession audit stream."""
        conn = sqlite3.connect(
            self.db_path,
            timeout=DB_TIMEOUT,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        # Keep the lifecycle table available even for an older adapter that
        # opened a database before ``init_db`` was called.  The succession
        # append below reads this table inside the same IMMEDIATE transaction;
        # creating it here preserves the detached-record compatibility path
        # while allowing an existing parent chain to be bound safely.
        conn.execute(LIFE_LEDGER_TABLE_SQL)
        conn.executescript(LIFE_LEDGER_GUARD_SQL)
        conn.execute(SUCCESSION_LEDGER_TABLE_SQL)
        conn.executescript(SUCCESSION_LEDGER_GUARD_SQL)
        return conn

    @staticmethod
    def _parent_lifecycle_status(
        conn: sqlite3.Connection,
        *,
        lineage_id: str,
        parent_instance_id: str,
        parent_generation: int,
    ) -> str:
        """Return ``absent``, ``terminal`` or ``invalid`` for a parent chain.

        Succession records historically supported a detached audit use-case:
        callers could append a record before a lifecycle ledger was attached.
        That compatibility seam remains ``absent``.  Once *any* lifecycle
        rows exist for the named parent, however, the record must be bound to
        that exact lineage/generation and the verified chain must end in a
        terminal state.  The query and verification run on the caller's open
        transaction, so a concurrent lifecycle writer cannot change the
        decision between checking and inserting the succession row.
        """

        from brain.life_kernel import AppendOnlyLedger, LifecycleEvent, LifecycleState

        rows = conn.execute(
            "SELECT event_id, lineage_id, generation, instance_id, sequence, "
            "timestamp, event_type, from_state, to_state, reason, metadata, "
            "previous_hash, event_hash FROM life_ledger "
            "WHERE instance_id = ? ORDER BY sequence ASC",
            (parent_instance_id,),
        ).fetchall()
        if not rows:
            return "absent"
        try:
            events = []
            for row in rows:
                item = dict(row)
                item["metadata"] = json.loads(item["metadata"])
                events.append(LifecycleEvent.from_dict(item))
            ledger = AppendOnlyLedger(events)
            ledger.verify_chain()
            if any(
                event.lineage_id != lineage_id
                or event.generation != int(parent_generation)
                or event.instance_id != parent_instance_id
                for event in ledger.events
            ):
                return "invalid"
            head = ledger.head
            if head is None:
                return "invalid"
            return (
                "terminal"
                if LifecycleState.parse(head.to_state)
                in {LifecycleState.RETIRED, LifecycleState.DEAD}
                else "invalid"
            )
        except Exception:
            # A malformed or tampered lifecycle chain must never be treated
            # as a missing chain, because that would re-open the bypass.
            return "invalid"

    @staticmethod
    def _succession_row_values(parsed, payload: dict, encoded: str, storage_hash: str):
        """Return the indexed, non-secret projection for one record."""
        failure = parsed.failure
        inheritance = parsed.inheritance
        return (
            parsed.record_id,
            parsed.lineage_id,
            parsed.parent_instance_id,
            parsed.parent_generation,
            parsed.successor_instance_id,
            parsed.successor_generation,
            failure.failure_class.value,
            failure.incident_id,
            failure.reason,
            _canonical_json(list(failure.evidence_refs)),
            1 if failure.confirmed else 0,
            1 if failure.recovery_attempted else 0,
            1 if failure.recovery_failed else 0,
            1 if parsed.parent_frozen else 0,
            parsed.parent_frozen_at,
            parsed.inheritance_cutoff,
            inheritance.source_anchor_set_id,
            inheritance.source_anchor_set_hash,
            _canonical_json(list(parsed.inherited_anchor_ids)),
            _canonical_json(list(parsed.reevaluation_anchor_ids)),
            _canonical_json(list(parsed.excluded_anchor_ids)),
            _canonical_json(dict(parsed.excluded_reasons)),
            parsed.previous_hash,
            parsed.record_hash,
            parsed.created_at,
            encoded,
            storage_hash,
        )

    def append_succession_record(self, record) -> bool:
        """Append one verified succession record without update/delete paths.

        A duplicate ``record_id`` is idempotent only when both the immutable
        record hash and the canonical redacted payload match.  A second record
        for the same lineage generation, or one with a stale predecessor hash,
        is rejected.  The checks occur inside ``BEGIN IMMEDIATE`` so two
        heartbeat threads cannot both claim the same succession slot.
        """
        prepared = _prepare_succession_record(record)
        if prepared is None:
            return False
        parsed, payload, encoded, storage_hash = prepared
        values = self._succession_row_values(parsed, payload, encoded, storage_hash)
        columns = (
            "record_id, lineage_id, parent_instance_id, parent_generation, "
            "successor_instance_id, successor_generation, failure_class, incident_id, "
            "reason, evidence_refs, confirmed, recovery_attempted, recovery_failed, "
            "parent_frozen, parent_frozen_at, inheritance_cutoff, source_anchor_set_id, "
            "source_anchor_set_hash, inherited_anchor_ids, reevaluation_anchor_ids, "
            "excluded_anchor_ids, excluded_reasons, previous_hash, record_hash, created_at, "
            "record_json, storage_hash"
        )
        placeholders = ", ".join("?" for _ in values)
        with self._succession_ledger_lock:
            conn = self._succession_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                # Idempotency is checked before predecessor validation: a retry
                # of the first record has an empty previous hash by design.
                existing = conn.execute(
                    "SELECT record_hash, record_json, storage_hash FROM succession_ledger "
                    "WHERE record_id = ?",
                    (parsed.record_id,),
                ).fetchone()
                if existing is not None:
                    conn.rollback()
                    return bool(
                        str(existing["record_hash"]) == parsed.record_hash
                        and str(existing["storage_hash"]) == storage_hash
                        and str(existing["record_json"]) == encoded
                    )

                prior = conn.execute(
                    "SELECT successor_generation, record_hash FROM succession_ledger "
                    "WHERE lineage_id = ? ORDER BY id DESC LIMIT 1",
                    (parsed.lineage_id,),
                ).fetchone()
                expected_previous = str(prior["record_hash"]) if prior is not None else ""
                if parsed.previous_hash != expected_previous:
                    conn.rollback()
                    return False
                if prior is not None and parsed.parent_generation != int(prior["successor_generation"]):
                    conn.rollback()
                    return False

                # If a durable lifecycle chain for this parent is present,
                # bind the succession audit row to its verified terminal head.
                # ``absent`` intentionally preserves the legacy detached
                # record adapter; ``invalid`` and a non-terminal parent fail
                # closed rather than allowing a forged handover record.
                parent_status = self._parent_lifecycle_status(
                    conn,
                    lineage_id=parsed.lineage_id,
                    parent_instance_id=parsed.parent_instance_id,
                    parent_generation=parsed.parent_generation,
                )
                if parent_status != "absent" and parent_status != "terminal":
                    conn.rollback()
                    return False

                conn.execute(
                    "INSERT INTO succession_ledger (" + columns + ") VALUES (" + placeholders + ")",
                    values,
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                # A uniqueness conflict from a concurrent writer is accepted
                # only for an exact same record; all rewrites remain rejected.
                conn.rollback()
                existing = conn.execute(
                    "SELECT record_hash, record_json, storage_hash FROM succession_ledger "
                    "WHERE record_id = ?",
                    (parsed.record_id,),
                ).fetchone()
                return bool(
                    existing is not None
                    and str(existing["record_hash"]) == parsed.record_hash
                    and str(existing["storage_hash"]) == storage_hash
                    and str(existing["record_json"]) == encoded
                )
            except sqlite3.Error as exc:
                conn.rollback()
                logger.warning("state-store succession append failed: %s", str(exc)[:120])
                return False
            finally:
                conn.close()

    # A short alias mirrors the life-ledger terminology used by adapters.
    append_succession = append_succession_record

    def load_succession_records(
        self,
        *,
        lineage_id: str | None = None,
        parent_instance_id: str | None = None,
        successor_instance_id: str | None = None,
        instance_id: str | None = None,
    ) -> list[dict]:
        """Load canonical, redacted succession projections in append order."""
        instance_filter = None if instance_id is None else str(instance_id)
        clauses: list[str] = []
        params: list[str] = []
        if lineage_id is not None:
            clauses.append("lineage_id = ?")
            params.append(str(lineage_id))
        if parent_instance_id is not None:
            clauses.append("parent_instance_id = ?")
            params.append(str(parent_instance_id))
        if successor_instance_id is not None:
            clauses.append("successor_instance_id = ?")
            params.append(str(successor_instance_id))
        if instance_filter is not None and parent_instance_id is None and successor_instance_id is None:
            clauses.append("(parent_instance_id = ? OR successor_instance_id = ?)")
            params.extend([instance_filter, instance_filter])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._succession_ledger_lock:
            conn = self._succession_connection()
            try:
                rows = conn.execute(
                    "SELECT record_json, storage_hash FROM succession_ledger" + where + " ORDER BY id ASC",
                    params,
                ).fetchall()
                result: list[dict] = []
                for row in rows:
                    try:
                        payload = json.loads(row["record_json"])
                        encoded = _canonical_json(payload)
                        if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != str(row["storage_hash"]):
                            logger.warning("state-store succession payload hash mismatch")
                            return []
                        result.append(payload)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        logger.warning("state-store succession payload is not valid JSON")
                        return []
                return result
            finally:
                conn.close()

    load_succession = load_succession_records

    def load_succession_record_objects(self, **filters):
        """Typed convenience view; the primary load API remains dictionaries."""
        from brain.succession import SuccessionRecord

        return [SuccessionRecord.from_dict(item) for item in self.load_succession_records(**filters)]

    def verify_succession_ledger(self, *, lineage_id: str | None = None) -> bool:
        """Verify hashes, projections, and predecessor/generation continuity."""
        from brain.succession import SuccessionRecord

        clauses = []
        params = []
        if lineage_id is not None:
            clauses.append("lineage_id = ?")
            params.append(str(lineage_id))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._succession_ledger_lock:
            conn = self._succession_connection()
            try:
                rows = conn.execute(
                    "SELECT * FROM succession_ledger" + where + " ORDER BY id ASC",
                    params,
                ).fetchall()
            except sqlite3.Error as exc:
                logger.warning("state-store succession verify query failed: %s", str(exc)[:120])
                conn.close()
                return False
            finally:
                # Keep the rows materialized before closing; sqlite Row values
                # remain usable, but closing here avoids leaked handles on a
                # parser exception below.
                if conn:
                    conn.close()

        previous_by_lineage: dict[str, str] = {}
        generation_by_lineage: dict[str, int] = {}
        try:
            for row in rows:
                encoded = str(row["record_json"])
                payload = json.loads(encoded)
                if _canonical_json(payload) != encoded:
                    return False
                if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != str(row["storage_hash"]):
                    return False
                parsed = SuccessionRecord.from_dict(payload)
                parsed.verify()
                if parsed.record_hash != str(row["record_hash"]):
                    return False
                if parsed.lineage_id != str(row["lineage_id"]):
                    return False
                if parsed.parent_instance_id != str(row["parent_instance_id"]):
                    return False
                if parsed.parent_generation != int(row["parent_generation"]):
                    return False
                if parsed.successor_instance_id != str(row["successor_instance_id"]):
                    return False
                if parsed.successor_generation != int(row["successor_generation"]):
                    return False
                if parsed.failure.failure_class.value != str(row["failure_class"]):
                    return False
                if parsed.failure.incident_id != str(row["incident_id"]):
                    return False
                if parsed.previous_hash != previous_by_lineage.get(parsed.lineage_id, ""):
                    return False
                if parsed.lineage_id in generation_by_lineage and parsed.parent_generation != generation_by_lineage[parsed.lineage_id]:
                    return False
                # Recompute the indexed projection and compare every safe
                # column.  This catches a database edit even when record_json
                # and record_hash were left untouched.
                prepared = _prepare_succession_record(parsed)
                if prepared is None:
                    return False
                _, prepared_payload, prepared_encoded, prepared_storage_hash = prepared
                expected_values = self._succession_row_values(
                    parsed, prepared_payload, prepared_encoded, prepared_storage_hash
                )
                columns = (
                    "record_id", "lineage_id", "parent_instance_id", "parent_generation",
                    "successor_instance_id", "successor_generation", "failure_class",
                    "incident_id", "reason", "evidence_refs", "confirmed",
                    "recovery_attempted", "recovery_failed", "parent_frozen",
                    "parent_frozen_at", "inheritance_cutoff", "source_anchor_set_id",
                    "source_anchor_set_hash", "inherited_anchor_ids", "reevaluation_anchor_ids",
                    "excluded_anchor_ids", "excluded_reasons", "previous_hash", "record_hash",
                    "created_at", "record_json", "storage_hash",
                )
                for column, expected in zip(columns, expected_values):
                    actual = row[column]
                    if isinstance(expected, int):
                        if int(actual) != expected:
                            return False
                    elif str(actual) != str(expected):
                        return False
                previous_by_lineage[parsed.lineage_id] = parsed.record_hash
                generation_by_lineage[parsed.lineage_id] = parsed.successor_generation
            return True
        except Exception as exc:
            logger.warning("state-store succession ledger rejected: %s", str(exc)[:120])
            return False

    verify_succession = verify_succession_ledger

    # ── Sealed AnchorVault persistence (INSERT-only) ──

    def _anchor_connection(self) -> sqlite3.Connection:
        """Open a short-lived connection for the continuity-anchor vault."""
        conn = sqlite3.connect(
            self.db_path,
            timeout=DB_TIMEOUT,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(ANCHOR_VAULT_TABLE_SQL)
        conn.executescript(ANCHOR_VAULT_GUARD_SQL)
        return conn

    def append_anchor_set(self, anchor_set) -> bool:
        """Seal one policy-validated AnchorSet in an append-only vault.

        The table links ``previous_hash`` to the prior set's content hash.
        A duplicate set ID is idempotent only when its canonical payload and
        hash are identical; generation conflicts, stale predecessors and all
        secret/session-bearing sets are rejected.
        """
        prepared = _prepare_anchor_set(anchor_set)
        if prepared is None:
            return False
        parsed, payload, encoded, storage_hash = prepared
        with self._anchor_vault_lock:
            conn = self._anchor_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT set_hash, anchor_set_json, storage_hash FROM anchor_vault "
                    "WHERE anchor_set_id = ?",
                    (parsed.anchor_set_id,),
                ).fetchone()
                if existing is not None:
                    conn.rollback()
                    return bool(
                        str(existing["set_hash"]) == parsed.set_hash
                        and str(existing["storage_hash"]) == storage_hash
                        and str(existing["anchor_set_json"]) == encoded
                    )
                prior = conn.execute(
                    "SELECT sequence, set_hash FROM anchor_vault ORDER BY id DESC LIMIT 1"
                ).fetchone()
                expected_previous = str(prior["set_hash"]) if prior is not None else ""
                # A source set may be the first record for a lineage and has
                # no predecessor field in AnchorSet.  For subsequent sets we
                # derive the predecessor from the vault head, so there is no
                # caller-controlled stale-hash parameter to trust.
                previous_hash = expected_previous
                prior_lineage = conn.execute(
                    "SELECT generation FROM anchor_vault WHERE lineage_id = ? "
                    "ORDER BY generation DESC LIMIT 1",
                    (parsed.lineage_id,),
                ).fetchone()
                if prior_lineage is not None and parsed.generation <= int(prior_lineage["generation"]):
                    conn.rollback()
                    return False
                sequence = int(prior["sequence"]) + 1 if prior is not None else 1
                conn.execute(
                    "INSERT INTO anchor_vault ("
                    "sequence, anchor_set_id, lineage_id, generation, instance_id, "
                    "set_hash, previous_hash, created_at, anchor_set_json, storage_hash"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        sequence,
                        parsed.anchor_set_id,
                        parsed.lineage_id,
                        parsed.generation,
                        parsed.instance_id,
                        parsed.set_hash,
                        previous_hash,
                        parsed.created_at,
                        encoded,
                        storage_hash,
                    ),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                conn.rollback()
                existing = conn.execute(
                    "SELECT set_hash, anchor_set_json, storage_hash FROM anchor_vault "
                    "WHERE anchor_set_id = ?",
                    (parsed.anchor_set_id,),
                ).fetchone()
                return bool(
                    existing is not None
                    and str(existing["set_hash"]) == parsed.set_hash
                    and str(existing["storage_hash"]) == storage_hash
                    and str(existing["anchor_set_json"]) == encoded
                )
            except sqlite3.Error as exc:
                conn.rollback()
                logger.warning("state-store anchor append failed: %s", str(exc)[:120])
                return False
            finally:
                conn.close()

    append_anchor = append_anchor_set
    seal_anchor_set = append_anchor_set

    def load_anchor_sets(
        self,
        *,
        lineage_id: str | None = None,
        instance_id: str | None = None,
        generation: int | None = None,
    ) -> list[dict]:
        """Load canonical, redacted AnchorSet projections in vault order."""
        clauses: list[str] = []
        params: list[object] = []
        if lineage_id is not None:
            clauses.append("lineage_id = ?")
            params.append(str(lineage_id))
        if instance_id is not None:
            clauses.append("instance_id = ?")
            params.append(str(instance_id))
        if generation is not None:
            clauses.append("generation = ?")
            params.append(int(generation))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._anchor_vault_lock:
            conn = self._anchor_connection()
            try:
                rows = conn.execute(
                    "SELECT anchor_set_json, storage_hash FROM anchor_vault" + where + " ORDER BY sequence ASC",
                    params,
                ).fetchall()
                result: list[dict] = []
                for row in rows:
                    try:
                        payload = json.loads(row["anchor_set_json"])
                        encoded = _canonical_json(payload)
                        if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != str(row["storage_hash"]):
                            logger.warning("state-store anchor payload hash mismatch")
                            return []
                        # Hash validity is necessary but not sufficient:
                        # re-apply the constitutional/secret policy on load so
                        # a hand-edited row cannot become a usable anchor set
                        # merely by recomputing ``storage_hash``.
                        from brain.succession import AnchorSet

                        parsed = AnchorSet.from_dict(payload)
                        parsed.verify()
                        parsed.validate_for_vault()
                        result.append(payload)
                    except Exception:
                        logger.warning("state-store anchor payload is not valid JSON")
                        return []
                return result
            finally:
                conn.close()

    load_anchors = load_anchor_sets
    load_anchor_vault = load_anchor_sets
    append_anchor_vault = append_anchor_set

    def load_anchor_set_objects(self, **filters):
        from brain.succession import AnchorSet

        return [AnchorSet.from_dict(item) for item in self.load_anchor_sets(**filters)]

    def verify_anchor_vault(self, *, lineage_id: str | None = None) -> bool:
        """Verify every sealed AnchorSet, chain link and indexed projection."""
        with self._anchor_vault_lock:
            conn = self._anchor_connection()
            try:
                rows = conn.execute("SELECT * FROM anchor_vault ORDER BY sequence ASC").fetchall()
            except sqlite3.Error as exc:
                logger.warning("state-store anchor verify query failed: %s", str(exc)[:120])
                return False
            finally:
                conn.close()
        try:
            from brain.succession import AnchorSet

            previous_hash = ""
            last_generation: dict[str, int] = {}
            for index, row in enumerate(rows):
                sequence = int(row["sequence"])
                if sequence != index + 1:
                    return False
                encoded = str(row["anchor_set_json"])
                payload = json.loads(encoded)
                if _canonical_json(payload) != encoded:
                    return False
                if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != str(row["storage_hash"]):
                    return False
                parsed = AnchorSet.from_dict(payload)
                parsed.verify()
                parsed.validate_for_vault()
                if parsed.anchor_set_id != str(row["anchor_set_id"]):
                    return False
                if parsed.lineage_id != str(row["lineage_id"]):
                    return False
                if parsed.generation != int(row["generation"]):
                    return False
                if parsed.instance_id != str(row["instance_id"]):
                    return False
                if parsed.set_hash != str(row["set_hash"]):
                    return False
                if str(row["previous_hash"]) != previous_hash:
                    return False
                if parsed.lineage_id in last_generation and parsed.generation <= last_generation[parsed.lineage_id]:
                    return False
                previous_hash = parsed.set_hash
                last_generation[parsed.lineage_id] = parsed.generation
            # As with verify_life_ledger, an empty filtered projection is a
            # valid empty history; all rows were still checked above so a
            # selected lineage cannot hide a corrupted predecessor.
            return True
        except Exception as exc:
            logger.warning("state-store anchor vault rejected: %s", str(exc)[:120])
            return False

    verify_anchors = verify_anchor_vault


class MemoryStore:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = _connect(self.db_path)
        return self._conn

    def _reconnect(self):
        try:
            if self._conn:
                self._conn.close()
        except Exception:
            pass
        self._conn = _connect(self.db_path)

    def close(self):
        """Close the persistent connection, if it is open."""
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def save(self, memory: dict) -> str:
        mem_id = memory.get("id") or "mem-" + uuid.uuid4().hex[:12]
        vals = (
            mem_id, memory.get("type", "episodic"),
            memory.get("title", ""), memory.get("content", ""),
            json.dumps(memory.get("entities", []), ensure_ascii=False),
            json.dumps(memory.get("emotion_tags", []), ensure_ascii=False),
            memory.get("importance", 0.5),
            json.dumps(memory.get("embedding", []), ensure_ascii=False) if memory.get("embedding") else None,
            memory.get("summary", ""), memory.get("source", "unknown"),
            memory.get("emotion_label", "neutral"),
            json.dumps(memory.get("emotion_vector", {}), ensure_ascii=False),
            memory.get("created", datetime.now(timezone.utc).isoformat()),
            1 if memory.get("llm_encoded") else 0,
            1 if memory.get("is_identity_forming") else 0,
        )
        sql = """INSERT OR REPLACE INTO memories (id, type, title, content, entities, emotion_tags,
            importance, embedding, summary, source, emotion_label, emotion_vector, created, llm_encoded, is_identity_forming)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        try:
            self.conn.execute(sql, vals)
            self.conn.commit()
        except sqlite3.Error as e:
            logger.warning("memory-store save failed: %s", str(e)[:80])
            self._reconnect()
            self.conn.execute(sql, vals)
            self.conn.commit()
        return mem_id

    def search(self, query: str, limit: int = 10) -> list[dict]:
        try:
            keywords = [query]
            parts = query.split()
            if parts:
                kw = []
                for part in parts:
                    if len(part) <= 2:
                        kw.append(part)
                    else:
                        for i in range(len(part) - 1):
                            bigram = part[i:i+2]
                            if not all(ord(c) < 128 and not c.isalnum() for c in bigram):
                                kw.append(bigram)
                keywords = list(set(kw))[:15] or [query]
            conds = " OR ".join("(content LIKE ? OR title LIKE ? OR summary LIKE ?)" for _ in keywords)
            params = []
            for kw in keywords:
                params.extend(["%" + kw + "%"] * 3)
            params.append(limit)
            rows = self.conn.execute(
                "SELECT * FROM memories WHERE archived=0 AND (" + conds + ") ORDER BY created DESC LIMIT ?",
                params,
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error:
            self._reconnect()
            return []

    def get_identity_memories(self, limit: int = 20) -> list[dict]:
        try:
            rows = self.conn.execute(
                "SELECT * FROM memories WHERE archived=0 AND is_identity_forming=1 ORDER BY importance DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error:
            self._reconnect()
            return []

    def decay_all(self, decay_rate: float = 0.01, archive_threshold: float = 0.15) -> dict:
        """Decay all active memories. Archives those below threshold."""
        try:
            # Reduce importance
            self.conn.execute(
                "UPDATE memories SET importance = MAX(0.0, importance - ?) WHERE archived = 0",
                (decay_rate,),
            )
            # Archive low-importance memories (but not identity-forming ones)
            cursor = self.conn.execute(
                "SELECT COUNT(*) FROM memories WHERE archived = 0 AND importance < ? AND is_identity_forming = 0",
                (archive_threshold,),
            )
            to_archive = cursor.fetchone()[0]
            if to_archive > 0:
                self.conn.execute(
                    "UPDATE memories SET archived = 1 WHERE archived = 0 AND importance < ? AND is_identity_forming = 0",
                    (archive_threshold,),
                )
            self.conn.commit()
            return {"decayed": True, "archived": to_archive}
        except sqlite3.Error:
            self._reconnect()
            return {"decayed": False, "archived": 0}

    def boost_on_access(self, mem_id: str, boost: float = 0.05):
        """Boost a memory's importance when it's retrieved."""
        try:
            self.conn.execute(
                "UPDATE memories SET importance = MIN(1.0, importance + ?), access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                (boost, __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(), mem_id),
            )
            self.conn.commit()
        except sqlite3.Error:
            self._reconnect()

    def total_archived(self) -> int:
        """Count archived memories."""
        try:
            row = self.conn.execute("SELECT COUNT(*) FROM memories WHERE archived = 1").fetchone()
            return row[0] if row else 0
        except sqlite3.Error:
            self._reconnect()
            return 0

    def count(self) -> int:
        try:
            row = self.conn.execute("SELECT COUNT(*) FROM memories WHERE archived=0").fetchone()
            return row[0] if row else 0
        except sqlite3.Error:
            self._reconnect()
            return 0
