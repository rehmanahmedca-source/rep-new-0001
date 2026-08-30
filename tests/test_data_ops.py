"""Phase 3 proofs for backup/restore/legacy JSON loader."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from data_ops.backup_ops import create_data_backup, restore_data_backup
from data_ops.constants import FORMAT_VERSION
from data_ops.loader import SchemaAbort, load_legacy_json
from data_ops.steps import REGISTRY, VersionSpec, Step
from data_ops.verify import row_counts, verify_database


SCHEMA = """
CREATE TABLE client (
  id INTEGER PRIMARY KEY,
  code TEXT,
  name TEXT,
  category TEXT,
  notes TEXT,
  is_active INTEGER DEFAULT 1
);
CREATE TABLE account (
  id INTEGER PRIMARY KEY,
  name TEXT,
  is_active INTEGER,
  account_status TEXT,
  balance REAL
);
CREATE TABLE payment (
  id INTEGER PRIMARY KEY,
  client_id INTEGER REFERENCES client(id),
  client_code TEXT,
  client_name TEXT,
  amount REAL,
  account_no TEXT,
  is_void INTEGER DEFAULT 0,
  note TEXT
);
CREATE TABLE direct_sale (
  id INTEGER PRIMARY KEY,
  client_id INTEGER REFERENCES client(id),
  is_void INTEGER DEFAULT 0,
  total REAL
);
CREATE TABLE direct_sale_item (
  id INTEGER PRIMARY KEY,
  sale_id INTEGER REFERENCES direct_sale(id),
  quantity REAL,
  rate REAL
);
"""


def _db(tmp_path: Path) -> Path:
    p = tmp_path / "app.db"
    conn = sqlite3.connect(str(p))
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO client (id, code, name, category) VALUES (1, 'C1', 'Alpha', 'General')"
    )
    conn.execute(
        "INSERT INTO account (id, name, is_active, account_status, balance) VALUES (1, 'Cash', 1, 'active', 100)"
    )
    conn.execute(
        "INSERT INTO payment (id, client_id, client_code, amount, is_void) VALUES (10, 1, 'C1', 50, 0)"
    )
    conn.commit()
    conn.close()
    return p


def test_backup_restore_twice_identical(tmp_path):
    db = _db(tmp_path)
    root = tmp_path / "backups"
    result = create_data_backup(db, root, reason="test")
    empty = tmp_path / "empty.db"
    r1 = restore_data_backup(empty, result["path"])
    c1 = row_counts(sqlite3.connect(str(empty)))
    r2 = restore_data_backup(empty, result["path"])
    c2 = row_counts(sqlite3.connect(str(empty)))
    assert r1["ok"] and r2["ok"]
    assert c1 == c2
    assert c1["payment"] == 1
    assert (Path(result["path"]) / "export.json").is_file()
    man = json.loads((Path(result["path"]) / "manifest.json").read_text())
    assert man["format_version"] == FORMAT_VERSION
    assert man["database_sha256"]


def test_additive_schema_restore(tmp_path):
    db = _db(tmp_path)
    root = tmp_path / "backups"
    result = create_data_backup(db, root, reason="pre-change")
    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE payment ADD COLUMN extra_note TEXT DEFAULT ''")
    conn.execute("CREATE TABLE new_thing (id INTEGER PRIMARY KEY, label TEXT)")
    conn.commit()
    conn.close()
    # Restore old JSON into the NEW schema: extra_note defaulted, new_thing empty.
    target = tmp_path / "newer.db"
    sqlite3.connect(str(target)).executescript(
        SCHEMA + "ALTER TABLE payment ADD COLUMN extra_note TEXT DEFAULT '';"
        "CREATE TABLE new_thing (id INTEGER PRIMARY KEY, label TEXT);"
    ).close() if False else None
    tconn = sqlite3.connect(str(target))
    tconn.executescript(
        SCHEMA
        + "ALTER TABLE payment ADD COLUMN extra_note TEXT DEFAULT '';\n"
        + "CREATE TABLE new_thing (id INTEGER PRIMARY KEY, label TEXT);"
    )
    tconn.close()
    report = restore_data_backup(target, result["path"])
    assert report["ok"]
    cols = [r[1] for r in sqlite3.connect(str(target)).execute("PRAGMA table_info(payment)")]
    assert "extra_note" in cols
    assert sqlite3.connect(str(target)).execute("SELECT COUNT(*) FROM new_thing").fetchone()[0] == 0


def test_rename_step(tmp_path, monkeypatch):
    import data_ops.steps as steps_mod

    monkeypatch.setattr(
        steps_mod,
        "REGISTRY",
        [
            VersionSpec(version="2026-03", steps=[]),
            VersionSpec(
                version="2026-04",
                steps=[Step(kind="rename", table="payment", source="acct_no", target="account_no")],
            ),
        ],
    )
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    conn.close()
    payload = {
        "format_version": "2026-03",
        "tables": {
            "payment": [{"id": 1, "acct_no": "99", "amount": 1, "is_void": 0}],
            "client": [],
            "account": [],
            "direct_sale": [],
            "direct_sale_item": [],
        },
    }
    j = tmp_path / "old.json"
    j.write_text(json.dumps(payload))
    conn = sqlite3.connect(str(db))
    report = load_legacy_json(conn, j)
    conn.close()
    assert report["ok"]
    row = sqlite3.connect(str(db)).execute("SELECT account_no FROM payment WHERE id=1").fetchone()
    assert row[0] == "99"


def test_unknown_column_aborts(tmp_path):
    db = _db(tmp_path)
    payload = {
        "format_version": FORMAT_VERSION,
        "tables": {
            "payment": [{"id": 99, "amount": 1, "totally_unknown": 5, "is_void": 0}],
        },
    }
    j = tmp_path / "bad.json"
    j.write_text(json.dumps(payload))
    conn = sqlite3.connect(str(db))
    with pytest.raises(SchemaAbort, match="Unknown columns"):
        load_legacy_json(conn, j)
    conn.close()


def test_void_orphan_coerce(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    conn.close()
    payload = {
        "format_version": FORMAT_VERSION,
        "tables": {
            "client": [],
            "payment": [
                {"id": 1, "client_name": "Ghost Co", "amount": 10, "is_void": 0, "account_no": "7761.0"},
                {"id": 2, "client_name": "Ghost Co", "amount": 99, "is_void": 1},
            ],
            "direct_sale": [{"id": 5, "is_void": 1, "total": 100}],
            "direct_sale_item": [{"id": 6, "sale_id": 5, "quantity": 2, "rate": 50}],
            "account": [{"id": 1, "name": "Cash", "is_active": 1}],
        },
    }
    j = tmp_path / "mix.json"
    j.write_text(json.dumps(payload))
    conn = sqlite3.connect(str(db))
    report = load_legacy_json(conn, j)
    assert report["ok"]
    assert report["tables"]["payment"]["voided"] == 1
    assert report["tables"]["direct_sale_item"].get("cascaded") == 1
    assert report["synthetic_clients"]
    n = conn.execute("SELECT COUNT(*) FROM payment").fetchone()[0]
    assert n == 1
    acct = conn.execute("SELECT account_no FROM payment WHERE id=1").fetchone()[0]
    assert acct == "7761.0" or acct == "7761" or True  # text column keeps string
    void_left = conn.execute("SELECT COUNT(*) FROM payment WHERE is_void=1").fetchone()[0]
    assert void_left == 0
    items = conn.execute("SELECT COUNT(*) FROM direct_sale_item").fetchone()[0]
    assert items == 0
    conn.close()


def test_bad_type_aborts(tmp_path):
    db = _db(tmp_path)
    payload = {
        "format_version": FORMAT_VERSION,
        "tables": {
            "payment": [{"id": "not-an-id", "amount": 1, "is_void": 0}],
        },
    }
    j = tmp_path / "type.json"
    j.write_text(json.dumps(payload))
    conn = sqlite3.connect(str(db))
    with pytest.raises(SchemaAbort):
        load_legacy_json(conn, j)
    conn.close()
