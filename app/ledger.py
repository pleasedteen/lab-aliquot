#!/usr/bin/env python3
"""本地样品台账核心：SQLite 存储 + 精确分数运算。

所有写入操作在单个事务内完成：先完整校验，再落库；任何异常都会回滚，
不会出现“原管扣了量、新管却没建出来”的半截状态。
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 2


class LedgerError(Exception):
    status = 400

    def __init__(self, message: str, details: Iterable[str] | None = None):
        super().__init__(message)
        self.message = message
        self.details = list(details or [])

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": self.message}
        if self.details:
            out["details"] = self.details
        return out


class NotFoundError(LedgerError):
    status = 404


class ConflictError(LedgerError):
    status = 409


# ---------------------------------------------------------------- 数量与编号


def parse_qty(value: Any, field: str = "用量") -> Fraction:
    """把用户输入的微升数解析成精确分数。

    支持 12.5、0.125、1/3、3/7 等写法；小数按十进制字面值转换，
    不经过二进制浮点，因此 0.1 + 0.2 精确等于 0.3。
    """
    if value is None or isinstance(value, bool):
        raise LedgerError(f"{field}未填写或格式不正确")
    if isinstance(value, int):
        frac = Fraction(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise LedgerError(f"{field}必须是有限数字")
        frac = Fraction(Decimal(str(value)))
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise LedgerError(f"{field}未填写")
        try:
            if "/" in text:
                parts = text.split("/")
                if len(parts) != 2:
                    raise ValueError("slash")
                num = Decimal(parts[0].strip())
                den = Decimal(parts[1].strip())
                if not num.is_finite() or not den.is_finite():
                    raise ValueError("not finite")
                if den == 0:
                    raise ValueError("zero")
                frac = Fraction(num) / Fraction(den)
            else:
                dec = Decimal(text)
                if not dec.is_finite():
                    raise ValueError("not finite")
                frac = Fraction(dec)
        except (InvalidOperation, ValueError, ZeroDivisionError, ArithmeticError):
            raise LedgerError(f"{field}“{value}”无法识别，请填数字或分数，例如 12.5 或 1/3")
    else:
        raise LedgerError(f"{field}格式不正确")
    if frac <= 0:
        raise LedgerError(f"{field}必须大于 0")
    return frac


def qty_str(frac: Fraction) -> str:
    """规范化的精确分数文本，整数不带分母；0 记作 "0"，负号原样保留。"""
    if frac.denominator == 1:
        return str(frac.numerator)
    return f"{frac.numerator}/{frac.denominator}"


def qty_display(frac: Fraction) -> str:
    """给界面看的写法：能有限小数化就写小数，否则保留精确分数。"""
    den = frac.denominator
    twos = fives = 0
    while den % 2 == 0:
        den //= 2
        twos += 1
    while den % 5 == 0:
        den //= 5
        fives += 1
    if den == 1:
        places = max(twos, fives)
        if places == 0:
            return str(frac.numerator)
        with localcontext() as ctx:
            ctx.prec = max(24, len(str(abs(frac.numerator))) + places + 4)
            dec = Decimal(frac.numerator) / Decimal(frac.denominator)
        text = format(dec, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"
    return qty_str(frac)


def qty_decimal_approx(frac: Fraction, places: int = 6) -> str:
    with localcontext() as ctx:
        ctx.prec = max(30, len(str(abs(frac.numerator))) + places + 8)
        dec = Decimal(frac.numerator) / Decimal(frac.denominator)
    text = format(dec, f".{places}f").rstrip("0").rstrip(".")
    return text or "0"


def percent_display(frac: Fraction, places: int = 4) -> str:
    with localcontext() as ctx:
        ctx.prec = 30
        dec = Decimal(frac.numerator) * Decimal(100) / Decimal(frac.denominator)
    text = format(dec, f".{places}f").rstrip("0").rstrip(".")
    return text or "0"


def qty_payload(frac: Fraction) -> dict[str, str]:
    return {
        "exact": qty_str(frac),
        "display": qty_display(frac),
        "decimal": qty_decimal_approx(frac),
    }


def parse_code(value: Any, field: str = "编号") -> str:
    if not isinstance(value, str):
        raise LedgerError(f"{field}未填写")
    code = value.strip()
    if not code:
        raise LedgerError(f"{field}未填写")
    if len(code) > 64:
        raise LedgerError(f"{field}不能超过 64 个字符")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in code):
        raise LedgerError(f"{field}不能包含控制字符")
    return code


def parse_batch(value: Any) -> str:
    if not isinstance(value, str):
        raise LedgerError("来样批次未填写")
    batch = value.strip()
    if not batch:
        raise LedgerError("来样批次未填写")
    if len(batch) > 128:
        raise LedgerError("来样批次不能超过 128 个字符")
    return batch


def parse_note(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise LedgerError("备注必须是文本")
    note = value.strip()
    if len(note) > 2000:
        raise LedgerError("备注不能超过 2000 个字符")
    return note


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sort_composition(mapping: dict[int, Fraction]) -> dict[str, str]:
    return {str(k): qty_str(mapping[k]) for k in sorted(mapping)}


def _composition_readable(
    comp_map: dict[str, str],
    tubes_by_id: dict[int, sqlite3.Row],
) -> list[dict[str, Any]]:
    result = []
    for key, value in sorted(comp_map.items(), key=lambda kv: int(kv[0])):
        intake_id = int(key)
        share = Fraction(value)
        intake = tubes_by_id.get(intake_id)
        result.append(
            {
                "intake_id": intake_id,
                "intake_code": intake["code"] if intake is not None else None,
                "batch": intake["batch"] if intake is not None else None,
                "share": qty_str(share),
                "share_display": qty_display(share),
                "percent": percent_display(share),
            }
        )
    return result


def _composition_text(comp_list: Iterable[dict[str, Any]]) -> str:
    parts = []
    for item in comp_list:
        label = f"{item.get('batch') or '?'}/{item.get('intake_code') or '?'}"
        parts.append(f"{label}:{item.get('percent')}%")
    return "; ".join(parts)


# ---------------------------------------------------------------- 数据库


DDL = """
CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN (
        'intake', 'split', 'retire', 'recall', 'lyophilize', 'reconstitute'
    )),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tubes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL COLLATE NOCASE UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('intake', 'pooled')),
    batch TEXT,
    initial_volume TEXT NOT NULL,
    initial_equivalent TEXT NOT NULL DEFAULT '0',
    composition TEXT NOT NULL,
    created_op INTEGER NOT NULL REFERENCES operations(id),
    created_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tube_moves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    op_id INTEGER NOT NULL REFERENCES operations(id),
    tube_id INTEGER NOT NULL REFERENCES tubes(id),
    position INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('create', 'take', 'dry', 'reconstitute')),
    volume TEXT NOT NULL,
    equivalent TEXT NOT NULL DEFAULT '0'
);

CREATE TABLE IF NOT EXISTS op_inputs (
    op_id INTEGER NOT NULL REFERENCES operations(id),
    tube_id INTEGER NOT NULL REFERENCES tubes(id),
    amount TEXT NOT NULL,
    equivalent TEXT NOT NULL DEFAULT '0',
    position INTEGER NOT NULL,
    PRIMARY KEY (op_id, position)
);

CREATE TABLE IF NOT EXISTS op_outputs (
    op_id INTEGER NOT NULL REFERENCES operations(id),
    tube_id INTEGER NOT NULL REFERENCES tubes(id),
    amount TEXT NOT NULL,
    equivalent TEXT NOT NULL DEFAULT '0',
    position INTEGER NOT NULL,
    PRIMARY KEY (op_id, position)
);

CREATE TABLE IF NOT EXISTS retire_items (
    op_id INTEGER NOT NULL REFERENCES operations(id),
    tube_id INTEGER NOT NULL REFERENCES tubes(id),
    discarded TEXT NOT NULL,
    discarded_equivalent TEXT NOT NULL DEFAULT '0',
    form TEXT NOT NULL DEFAULT 'liquid',
    position INTEGER NOT NULL,
    PRIMARY KEY (op_id, position)
);

CREATE TABLE IF NOT EXISTS process_items (
    op_id INTEGER NOT NULL REFERENCES operations(id),
    tube_id INTEGER NOT NULL REFERENCES tubes(id),
    position INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('dry', 'reconstitute')),
    form_before TEXT NOT NULL,
    volume_before TEXT NOT NULL,
    equivalent_before TEXT NOT NULL,
    form_after TEXT NOT NULL,
    volume_after TEXT NOT NULL,
    equivalent_after TEXT NOT NULL,
    PRIMARY KEY (op_id, position)
);

CREATE TABLE IF NOT EXISTS recalls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    op_id INTEGER NOT NULL UNIQUE REFERENCES operations(id),
    batch TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recall_items (
    recall_id INTEGER NOT NULL REFERENCES recalls(id),
    tube_id INTEGER NOT NULL REFERENCES tubes(id),
    code TEXT NOT NULL,
    remaining TEXT NOT NULL,
    equivalent TEXT NOT NULL DEFAULT '0',
    form TEXT NOT NULL DEFAULT 'liquid',
    composition TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (recall_id, position)
);

CREATE INDEX IF NOT EXISTS idx_op_inputs_tube ON op_inputs(tube_id);
CREATE INDEX IF NOT EXISTS idx_op_outputs_tube ON op_outputs(tube_id);
CREATE INDEX IF NOT EXISTS idx_retire_tube ON retire_items(tube_id);
CREATE INDEX IF NOT EXISTS idx_recall_items_tube ON recall_items(tube_id);
CREATE INDEX IF NOT EXISTS idx_tubes_batch ON tubes(batch);
CREATE INDEX IF NOT EXISTS idx_tubes_created_op ON tubes(created_op);
CREATE INDEX IF NOT EXISTS idx_tube_moves_tube ON tube_moves(tube_id, op_id, position);
CREATE INDEX IF NOT EXISTS idx_process_items_tube ON process_items(tube_id);
"""

OPERATIONS_DDL = """
CREATE TABLE operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN (
        'intake', 'split', 'retire', 'recall', 'lyophilize', 'reconstitute'
    )),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
)
"""



class Ledger:
    """样品台账。所有金额均以 Fraction 精确计算，只在校验通过后写入。"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        # 仅供测试：写入过程中注入故障，验证事务回滚。
        self._fault_injector = None
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # -- connection helpers -------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise RuntimeError(
                        f"数据库版本 {version} 高于本工具支持的 {SCHEMA_VERSION}"
                    )
                conn.executescript(DDL)
                if version < 2:
                    self._migrate_v1_to_v2(conn)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            finally:
                conn.close()

    @staticmethod
    def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    @classmethod
    def _migrate_v1_to_v2(cls, conn: sqlite3.Connection) -> None:
        """v1 的库没有冻干/复溶和原样折算量：补齐列，并补记流水。

        v1 里所有管都是液体，液体状态下原样折算量就等于体积，
        所以历史数据的 `initial_equivalent = initial_volume`、每笔取用折算量 = 取用体积；
        成品折算量则用“初始折算量 - 此前已取走折算量”回填，保证折算量守衡。

        迁移按实际列判断是否已经做过，全新库（DDL 已是 v2）会直接跳过。
        """
        needs_tubes = "initial_equivalent" not in cls._column_names(conn, "tubes")
        needs_retire = "discarded_equivalent" not in cls._column_names(conn, "retire_items")
        needs_inputs = "equivalent" not in cls._column_names(conn, "op_inputs")
        needs_outputs = "equivalent" not in cls._column_names(conn, "op_outputs")
        needs_recalls = "equivalent" not in cls._column_names(conn, "recall_items")
        needs_ops = "lyophilize" not in (
            conn.execute("SELECT sql FROM sqlite_master WHERE name = 'operations'").fetchone()["sql"]
            or ""
        )
        if not (
            needs_tubes or needs_retire or needs_inputs or needs_outputs
            or needs_recalls or needs_ops
        ):
            return
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            if needs_tubes:
                conn.execute(
                    "ALTER TABLE tubes ADD COLUMN initial_equivalent TEXT NOT NULL DEFAULT '0'"
                )
            conn.execute("UPDATE tubes SET initial_equivalent = initial_volume")
            if needs_retire:
                conn.execute(
                    "ALTER TABLE retire_items ADD COLUMN discarded_equivalent "
                    "TEXT NOT NULL DEFAULT '0'"
                )
                conn.execute(
                    "ALTER TABLE retire_items ADD COLUMN form TEXT NOT NULL DEFAULT 'liquid'"
                )
            conn.execute("UPDATE retire_items SET discarded_equivalent = discarded")
            if needs_inputs:
                conn.execute(
                    "ALTER TABLE op_inputs ADD COLUMN equivalent TEXT NOT NULL DEFAULT '0'"
                )
            conn.execute("UPDATE op_inputs SET equivalent = amount")
            if needs_outputs:
                conn.execute(
                    "ALTER TABLE op_outputs ADD COLUMN equivalent TEXT NOT NULL DEFAULT '0'"
                )
            if needs_recalls:
                conn.execute(
                    "ALTER TABLE recall_items ADD COLUMN equivalent TEXT NOT NULL DEFAULT '0'"
                )
                conn.execute(
                    "ALTER TABLE recall_items ADD COLUMN form TEXT NOT NULL DEFAULT 'liquid'"
                )
            conn.execute("UPDATE recall_items SET equivalent = remaining")

            # CHECK 约束不能就地放宽，只能重建 operations 表。
            if needs_ops:
                conn.execute(OPERATIONS_DDL.replace("CREATE TABLE operations", "CREATE TABLE operations_v2"))
                conn.execute(
                    "INSERT INTO operations_v2(id, kind, note, created_at) "
                    "SELECT id, kind, note, created_at FROM operations"
                )
                conn.execute("DROP TABLE operations")
                conn.execute("ALTER TABLE operations_v2 RENAME TO operations")

            # 回填流水（如果之前没有）：历史管都是液体，折算量 = 体积。
            if conn.execute("SELECT 1 FROM tube_moves LIMIT 1").fetchone() is None:
                taken: dict[int, Fraction] = defaultdict(Fraction)
                for row in conn.execute(
                    "SELECT op_id, tube_id, amount, position FROM op_inputs "
                    "ORDER BY op_id, position"
                ).fetchall():
                    amount = Fraction(row["amount"])
                    conn.execute(
                        "INSERT INTO tube_moves(op_id, tube_id, kind, volume, equivalent, position) "
                        "VALUES (?, ?, 'take', ?, ?, ?)",
                        (
                            row["op_id"],
                            row["tube_id"],
                            qty_str(-amount),
                            qty_str(-amount),
                            row["position"],
                        ),
                    )
                    taken[row["tube_id"]] += amount
                tubes = {
                    row["id"]: Fraction(row["initial_equivalent"])
                    for row in conn.execute(
                        "SELECT id, initial_equivalent FROM tubes"
                    ).fetchall()
                }
                created: dict[int, Fraction] = {}
                for row in conn.execute(
                    "SELECT o.op_id, o.tube_id, o.amount, o.position FROM op_outputs o "
                    "ORDER BY o.op_id, o.position"
                ).fetchall():
                    amount = Fraction(row["amount"])
                    key = row["tube_id"]
                    if key in created:
                        # 一支管只建一次，后续 op_outputs 是分装到既有管的历史脏数据，跳过。
                        continue
                    remaining_before = tubes.get(key, amount) - taken.get(key, Fraction(0))
                    equivalent = max(remaining_before, Fraction(0))
                    created[key] = amount
                    conn.execute(
                        "INSERT INTO tube_moves(op_id, tube_id, kind, volume, equivalent, position) "
                        "VALUES (?, ?, 'create', ?, ?, ?)",
                        (
                            row["op_id"],
                            key,
                            qty_str(amount),
                            qty_str(equivalent),
                            row["position"],
                        ),
                    )
            conn.commit()
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

    @contextmanager
    def _transaction(self):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                    conn.execute("COMMIT")
                except BaseException:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
            finally:
                conn.close()

    @contextmanager
    def _read(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _fault(self, stage: str, index: int = 0) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage, index)

    # -- low-level lookups --------------------------------------------------

    @staticmethod
    def _tube_by_code(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM tubes WHERE code = ? COLLATE NOCASE", (code,)
        ).fetchone()

    @staticmethod
    def _balance_map(
        conn: sqlite3.Connection, cutoff: int | None
    ) -> dict[int, dict[str, Fraction]]:
        """每支管当前（或指定时点）的结存量：体积与折痕量各一条流水账。

        体积按流水增减：建管 +初始，取样 -相应量，冻干 -冻干前体积（归零），
        复溶 +实测体积。折痕量只随取样减少；冻干、复溶都不改变它。
        报废只记录废弃量，不改变剩余量（保留报废当时的状态）。
        这样体积与折痕量是两本平行的账，互不推导。
        """
        sql = "SELECT tube_id, kind, volume, equivalent FROM tube_moves"
        params: tuple = ()
        if cutoff is not None:
            sql += " WHERE op_id <= ?"
            params = (cutoff,)
        balance: dict[int, dict[str, Fraction]] = {}
        for row in conn.execute(sql, params).fetchall():
            bucket = balance.setdefault(
                row["tube_id"], {"volume": Fraction(0), "equivalent": Fraction(0)}
            )
            bucket["volume"] += Fraction(row["volume"])
            bucket["equivalent"] += Fraction(row["equivalent"])
        return balance

    @staticmethod
    def _retire_map(conn: sqlite3.Connection, cutoff: int | None) -> dict[int, dict[str, Any]]:
        sql = (
            "SELECT r.tube_id, r.discarded, r.discarded_equivalent, r.form, r.op_id, "
            "o.created_at FROM retire_items r JOIN operations o ON o.id = r.op_id"
        )
        params: tuple = ()
        if cutoff is not None:
            sql += " WHERE r.op_id <= ?"
            params = (cutoff,)
        result: dict[int, dict[str, Any]] = {}
        for row in conn.execute(sql, params).fetchall():
            entry = result.setdefault(
                row["tube_id"],
                {
                    "op_id": row["op_id"],
                    "created_at": row["created_at"],
                    "discarded": Fraction(0),
                    "discarded_equivalent": Fraction(0),
                    "form": row["form"],
                },
            )
            entry["discarded"] += Fraction(row["discarded"])
            entry["discarded_equivalent"] += Fraction(row["discarded_equivalent"])
        return result

    @staticmethod
    def _form_map(
        conn: sqlite3.Connection, cutoff: int | None
    ) -> dict[int, dict[str, Any]]:
        """每支管最新的物理状态（按 tube_moves 流水的最后一笔）。

        只有冻干 / 复溶改变物态：冻干 → 干样（粉），复溶 → 液体。
        取样（take）不改变物态：从复溶液里取样后剩下的仍是液体。
        """
        sql = (
            "SELECT m.tube_id, m.kind, m.op_id, m.volume, o.created_at "
            "FROM tube_moves m JOIN operations o ON o.id = m.op_id"
        )
        params: tuple = ()
        if cutoff is not None:
            sql += " WHERE m.op_id <= ?"
            params = (cutoff,)
        sql += " ORDER BY m.op_id, m.position, m.id"
        state: dict[int, dict[str, Any]] = {}
        for row in conn.execute(sql, params).fetchall():
            if row["kind"] == "dry":
                form = "dry"
            elif row["kind"] in ("create", "reconstitute"):
                form = "liquid"
            elif row["tube_id"] in state:
                form = state[row["tube_id"]]["form"]
            else:  # 没有流水却已存在，按来样液体兜底
                form = "liquid"
            state[row["tube_id"]] = {
                "form": form,
                "op_id": row["op_id"],
                "created_at": row["created_at"],
            }
        return state

    @staticmethod
    def _recall_map(conn: sqlite3.Connection, cutoff: int | None) -> dict[int, list[dict[str, Any]]]:
        sql = (
            "SELECT ri.tube_id, rc.id AS recall_id, rc.batch, rc.op_id, rc.created_at "
            "FROM recall_items ri JOIN recalls rc ON rc.id = ri.recall_id "
        )
        if cutoff is None:
            rows = conn.execute(sql).fetchall()
        else:
            rows = conn.execute(sql + "WHERE rc.op_id <= ?", (cutoff,)).fetchall()
        result: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            result[row["tube_id"]].append(
                {
                    "recall_id": row["recall_id"],
                    "batch": row["batch"],
                    "op_id": row["op_id"],
                    "created_at": row["created_at"],
                }
            )
        return result

    def _snapshot(self, conn: sqlite3.Connection, cutoff: int | None = None) -> dict[int, dict[str, Any]]:
        """返回 tube_id -> 当前（或指定操作序号时点）状态。

        体积与折痕量（原样折算量）是两本平行的账：冻干只改体积，不改折痕量；
        复溶按实测体积更新这两本账，折痕量保持不变。
        """
        if cutoff is None:
            tube_rows = conn.execute("SELECT * FROM tubes ORDER BY id").fetchall()
        else:
            tube_rows = conn.execute(
                "SELECT * FROM tubes WHERE created_op <= ? ORDER BY id", (cutoff,)
            ).fetchall()
        by_id = {row["id"]: row for row in tube_rows}
        balance = self._balance_map(conn, cutoff)
        retired = self._retire_map(conn, cutoff)
        recalled = self._recall_map(conn, cutoff)
        forms = self._form_map(conn, cutoff)
        snapshot: dict[int, dict[str, Any]] = {}
        for row in tube_rows:
            initial = Fraction(row["initial_volume"])
            initial_eq = Fraction(row["initial_equivalent"])
            entry = balance.get(row["id"], {})
            remaining = entry.get("volume", Fraction(0))
            remaining_eq = entry.get("equivalent", Fraction(0))
            retire_info = retired.get(row["id"])
            form_info = forms.get(row["id"])
            form = form_info["form"] if form_info else "liquid"
            if retire_info is not None:
                status = "retired"
            elif remaining <= 0 and form == "dry":
                # 干样体积账为 0 是“已去溶剂”，不是“用完了”，仍在库可复溶。
                status = "available"
            elif remaining <= 0:
                status = "exhausted"
            else:
                status = "available"
            comp_map = json.loads(row["composition"])
            comp_list = _composition_readable(comp_map, by_id)
            recalls = sorted(
                recalled.get(row["id"], []), key=lambda item: item["recall_id"]
            )
            snapshot[row["id"]] = {
                "id": row["id"],
                "code": row["code"],
                "kind": row["kind"],
                "batch": row["batch"],
                "form": form,
                "is_dry": form == "dry",
                "form_since_op": form_info["op_id"] if form_info else None,
                "initial_fraction": initial,
                "initial_equivalent_fraction": initial_eq,
                "remaining_fraction": remaining,
                "remaining_equivalent_fraction": remaining_eq,
                "initial_volume": qty_str(initial),
                "initial_volume_display": qty_display(initial),
                "initial_equivalent": qty_str(initial_eq),
                "initial_equivalent_display": qty_display(initial_eq),
                "remaining": qty_str(remaining),
                "remaining_display": qty_display(remaining),
                "remaining_equivalent": qty_str(remaining_eq),
                "remaining_equivalent_display": qty_display(remaining_eq),
                "status": status,
                "recalled": bool(recalls),
                "recalls": recalls,
                "retired_op": retire_info["op_id"] if retire_info else None,
                "retired_at": retire_info["created_at"] if retire_info else None,
                "discarded": qty_str(retire_info["discarded"]) if retire_info else None,
                "discarded_display": (
                    qty_display(retire_info["discarded"]) if retire_info else None
                ),
                "discarded_equivalent": (
                    qty_str(retire_info["discarded_equivalent"]) if retire_info else None
                ),
                "discarded_equivalent_display": (
                    qty_display(retire_info["discarded_equivalent"]) if retire_info else None
                ),
                "composition": comp_list,
                "created_op": row["created_op"],
                "created_at": row["created_at"],
                "note": row["note"],
            }
        return snapshot

    # -- serialization helpers ---------------------------------------------

    @staticmethod
    def _snapshot_item(item: dict[str, Any]) -> dict[str, Any]:
        base = {k: v for k, v in item.items() if not k.endswith("_fraction")}
        base["initial_volume_approx"] = qty_decimal_approx(item["initial_fraction"])
        base["remaining_approx"] = qty_decimal_approx(item["remaining_fraction"])
        base["initial_equivalent_approx"] = qty_decimal_approx(
            item["initial_equivalent_fraction"]
        )
        base["remaining_equivalent_approx"] = qty_decimal_approx(
            item["remaining_equivalent_fraction"]
        )
        return base

    def _composition_amounts(
        self, item: dict[str, Any]
    ) -> list[dict[str, Any]]:
        out = []
        for comp in item["composition"]:
            share = Fraction(comp["share"])
            out.append(
                {
                    **comp,
                    "amount_in_initial": qty_str(share * item["initial_fraction"]),
                    "amount_in_initial_display": qty_display(
                        share * item["initial_fraction"]
                    ),
                    "amount_in_remaining": qty_str(share * item["remaining_fraction"]),
                    "amount_in_remaining_display": qty_display(
                        share * item["remaining_fraction"]
                    ),
                    "equivalent_in_initial": qty_str(
                        share * item["initial_equivalent_fraction"]
                    ),
                    "equivalent_in_initial_display": qty_display(
                        share * item["initial_equivalent_fraction"]
                    ),
                    "equivalent_in_remaining": qty_str(
                        share * item["remaining_equivalent_fraction"]
                    ),
                    "equivalent_in_remaining_display": qty_display(
                        share * item["remaining_equivalent_fraction"]
                    ),
                }
            )
        return out

    @staticmethod
    def _latest_seq(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM operations").fetchone()
        return int(row["m"])

    # -- public reads -------------------------------------------------------

    def inventory(self, seq: int | None = None) -> dict[str, Any]:
        with self._read() as conn:
            latest = self._latest_seq(conn)
            if seq is not None:
                seq = int(seq)
                if seq < 0:
                    raise LedgerError("操作序号不能为负数")
                if seq > latest:
                    raise NotFoundError(f"操作序号 {seq} 不存在（当前最新为 {latest}）")
            snapshot = self._snapshot(conn, seq)
            items = [self._snapshot_item(v) for v in snapshot.values()]
            items.sort(key=lambda item: item["id"])
            counts = {
                "total": len(items),
                "available": sum(1 for i in items if i["status"] == "available"),
                "exhausted": sum(1 for i in items if i["status"] == "exhausted"),
                "retired": sum(1 for i in items if i["status"] == "retired"),
                "recalled_in_stock": sum(
                    1 for i in items if i["recalled"] and i["status"] == "available"
                ),
                "intake": sum(1 for i in items if i["kind"] == "intake"),
                "pooled": sum(1 for i in items if i["kind"] == "pooled"),
                "dry_in_stock": sum(
                    1 for i in items if i["is_dry"] and i["status"] == "available"
                ),
                "dry": sum(1 for i in items if i["is_dry"]),
            }
            batches = sorted(
                {
                    i["batch"]
                    for i in items
                    if i["kind"] == "intake" and i["batch"]
                }
            )
            return {
                "seq": latest if seq is None else seq,
                "latest_seq": latest,
                "counts": counts,
                "batches": batches,
                "tubes": items,
            }

    def state_at(self, seq: int) -> dict[str, Any]:
        return self.inventory(seq)

    def _operation_row(self, conn: sqlite3.Connection, op_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM operations WHERE id = ?", (op_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"操作 {op_id} 不存在")
        return row

    def operation_detail(self, op_id: int) -> dict[str, Any]:
        with self._read() as conn:
            return self._op_detail(conn, op_id)

    def _op_detail(self, conn: sqlite3.Connection, op_id: int) -> dict[str, Any]:
        op = self._operation_row(conn, op_id)
        inputs = conn.execute(
            "SELECT i.amount, i.equivalent, i.position, t.id AS tube_id, t.code, t.kind, t.batch "
            "FROM op_inputs i JOIN tubes t ON t.id = i.tube_id "
            "WHERE i.op_id = ? ORDER BY i.position",
            (op_id,),
        ).fetchall()
        outputs = conn.execute(
            "SELECT o.amount, o.equivalent, o.position, t.id AS tube_id, t.code, t.kind, t.batch "
            "FROM op_outputs o JOIN tubes t ON t.id = o.tube_id "
            "WHERE o.op_id = ? ORDER BY o.position",
            (op_id,),
        ).fetchall()
        retire_rows = conn.execute(
            "SELECT r.discarded, r.discarded_equivalent, r.form, r.position, "
            "t.id AS tube_id, t.code "
            "FROM retire_items r JOIN tubes t ON t.id = r.tube_id "
            "WHERE r.op_id = ? ORDER BY r.position",
            (op_id,),
        ).fetchall()
        process_rows = conn.execute(
            "SELECT p.*, t.code FROM process_items p JOIN tubes t ON t.id = p.tube_id "
            "WHERE p.op_id = ? ORDER BY p.position",
            (op_id,),
        ).fetchall()
        recall_row = conn.execute(
            "SELECT * FROM recalls WHERE op_id = ?", (op_id,)
        ).fetchone()
        detail: dict[str, Any] = {
            "id": op["id"],
            "seq": op["id"],
            "kind": op["kind"],
            "note": op["note"],
            "created_at": op["created_at"],
            "inputs": [
                {
                    "tube_id": row["tube_id"],
                    "code": row["code"],
                    "kind": row["kind"],
                    "batch": row["batch"],
                    "amount": row["amount"],
                    "amount_display": qty_display(Fraction(row["amount"]))
                    if row["amount"]
                    else None,
                    "equivalent": row["equivalent"],
                    "equivalent_display": qty_display(Fraction(row["equivalent"]))
                    if row["equivalent"]
                    else None,
                }
                for row in inputs
            ],
            "outputs": [
                {
                    "tube_id": row["tube_id"],
                    "code": row["code"],
                    "kind": row["kind"],
                    "batch": row["batch"],
                    "amount": row["amount"],
                    "amount_display": qty_display(Fraction(row["amount"]))
                    if row["amount"]
                    else None,
                    "equivalent": row["equivalent"],
                    "equivalent_display": qty_display(Fraction(row["equivalent"]))
                    if row["equivalent"]
                    else None,
                }
                for row in outputs
            ],
            "retired": [
                {
                    "tube_id": row["tube_id"],
                    "code": row["code"],
                    "form": row["form"],
                    "discarded": row["discarded"],
                    "discarded_display": qty_display(Fraction(row["discarded"])
                                                      if row["discarded"] else Fraction(0)),
                    "discarded_equivalent": row["discarded_equivalent"],
                    "discarded_equivalent_display": qty_display(
                        Fraction(row["discarded_equivalent"])
                        if row["discarded_equivalent"] else Fraction(0)
                    ),
                }
                for row in retire_rows
            ],
            "processing": [
                {
                    "tube_id": row["tube_id"],
                    "code": row["code"],
                    "action": row["action"],
                    "form_before": row["form_before"],
                    "volume_before": row["volume_before"],
                    "volume_before_display": qty_display(Fraction(row["volume_before"])),
                    "equivalent_before": row["equivalent_before"],
                    "equivalent_before_display": qty_display(
                        Fraction(row["equivalent_before"])
                    ),
                    "form_after": row["form_after"],
                    "volume_after": row["volume_after"],
                    "volume_after_display": qty_display(Fraction(row["volume_after"])),
                    "equivalent_after": row["equivalent_after"],
                    "equivalent_after_display": qty_display(
                        Fraction(row["equivalent_after"])
                    ),
                }
                for row in process_rows
            ],
            "recall": None,
        }
        if op["kind"] == "split":
            total_in = sum((Fraction(row["amount"]) for row in inputs), Fraction(0))
            total_out = sum((Fraction(row["amount"]) for row in outputs), Fraction(0))
            total_eq_in = sum((Fraction(row["equivalent"]) for row in inputs), Fraction(0))
            total_eq_out = sum((Fraction(row["equivalent"]) for row in outputs), Fraction(0))
            detail["total_in"] = qty_str(total_in)
            detail["total_in_display"] = qty_display(total_in)
            detail["total_out"] = qty_str(total_out)
            detail["total_out_display"] = qty_display(total_out)
            detail["total_equivalent_in"] = qty_str(total_eq_in)
            detail["total_equivalent_in_display"] = qty_display(total_eq_in)
            detail["total_equivalent_out"] = qty_str(total_eq_out)
            detail["total_equivalent_out_display"] = qty_display(total_eq_out)

            # 完全混匀后，每支成品按取样比例对应到本次的各支来源管。
            # 这是“每支成品实际从哪支管取了多少”的精确摊分（可为分数）。
            source_rows = [
                {
                    "tube_id": row["tube_id"],
                    "code": row["code"],
                    "amount": Fraction(row["amount"]),
                    "equivalent": Fraction(row["equivalent"]),
                    "share": Fraction(row["amount"]) / total_in,
                }
                for row in inputs
            ]
            output_breakdown = {}
            for row in outputs:
                out_amount = Fraction(row["amount"])
                out_eq = Fraction(row["equivalent"])
                output_breakdown[row["tube_id"]] = [
                    {
                        "tube_id": src["tube_id"],
                        "code": src["code"],
                        "amount": qty_str(out_amount * src["share"]),
                        "amount_display": qty_display(out_amount * src["share"]),
                        "equivalent": qty_str(out_eq * src["share"]),
                        "equivalent_display": qty_display(out_eq * src["share"]),
                        "share": qty_str(src["share"]),
                        "share_percent": percent_display(src["share"]),
                        "from_amount": qty_str(src["amount"]),
                        "from_amount_display": qty_display(src["amount"]),
                        "from_equivalent": qty_str(src["equivalent"]),
                        "from_equivalent_display": qty_display(src["equivalent"]),
                    }
                    for src in source_rows
                ]
            for item in detail["outputs"]:
                item["sources"] = output_breakdown.get(item["tube_id"], [])
            detail["input_total_map"] = {
                str(src["tube_id"]): {
                    "code": src["code"],
                    "amount": qty_str(src["amount"]),
                    "amount_display": qty_display(src["amount"]),
                    "equivalent": qty_str(src["equivalent"]),
                    "equivalent_display": qty_display(src["equivalent"]),
                    "share_percent": percent_display(src["share"]),
                }
                for src in source_rows
            }
        if recall_row is not None:
            items = conn.execute(
                "SELECT code, remaining, equivalent, form, composition, position "
                "FROM recall_items WHERE recall_id = ? ORDER BY position",
                (recall_row["id"],),
            ).fetchall()
            affected = []
            for row in items:
                comp = json.loads(row["composition"])
                affected.append(
                    {
                        "code": row["code"],
                        "form": row["form"],
                        "remaining": row["remaining"],
                        "remaining_display": qty_display(Fraction(row["remaining"])),
                        "equivalent": row["equivalent"],
                        "equivalent_display": qty_display(Fraction(row["equivalent"])),
                        "composition": comp,
                    }
                )
            detail["recall"] = {
                "id": recall_row["id"],
                "batch": recall_row["batch"],
                "note": recall_row["note"],
                "created_at": recall_row["created_at"],
                "affected_count": len(affected),
                "affected": affected,
            }
        return detail

    def history(self, limit: int | None = None) -> dict[str, Any]:
        with self._read() as conn:
            latest = self._latest_seq(conn)
            sql = "SELECT id FROM operations ORDER BY id"
            ids = [row["id"] for row in conn.execute(sql).fetchall()]
            if limit is not None:
                ids = ids[-int(limit):]
            ops = [self._op_detail(conn, op_id) for op_id in ids]
            return {"latest_seq": latest, "operations": ops}

    def tube_trace(self, code: str, seq: int | None = None) -> dict[str, Any]:
        code = parse_code(code, "管编号")
        with self._read() as conn:
            latest = self._latest_seq(conn)
            if seq is not None:
                seq = int(seq)
                if seq < 0 or seq > latest:
                    raise NotFoundError(f"操作序号 {seq} 不存在")
            row = self._tube_by_code(conn, code)
            if row is None:
                raise NotFoundError(f"找不到编号为 {code} 的管")
            if seq is not None and row["created_op"] > seq:
                raise NotFoundError(f"管 {code} 在操作 {seq} 时尚未建立")
            snapshot = self._snapshot(conn, seq)
            item = snapshot[row["id"]]
            composition = self._composition_amounts(item)
            tubes_by_id = {v["id"]: v for v in snapshot.values()}

            def node(tube_id: int, amount_taken: Fraction | None, depth: int = 0):
                tube = tubes_by_id[tube_id]
                base = {
                    "tube_id": tube["id"],
                    "code": tube["code"],
                    "kind": tube["kind"],
                    "batch": tube["batch"],
                    "status": tube["status"],
                    "form": tube["form"],
                    "amount_taken": qty_str(amount_taken) if amount_taken is not None else None,
                    "amount_taken_display": qty_display(amount_taken)
                    if amount_taken is not None
                    else None,
                    "equivalent_taken": None,
                    "composition": tube["composition"],
                }
                if tube["kind"] == "intake" or depth > 64:
                    return {**base, "children": []}
                producer = conn.execute(
                    "SELECT op_id FROM op_outputs WHERE tube_id = ?", (tube_id,)
                ).fetchone()
                children = []
                if producer is not None:
                    rows = conn.execute(
                        "SELECT i.tube_id, i.amount, i.equivalent, i.position FROM op_inputs i "
                        "WHERE i.op_id = ? ORDER BY i.position",
                        (producer["op_id"],),
                    ).fetchall()
                    for child in rows:
                        kids = node(child["tube_id"], Fraction(child["amount"]), depth + 1)
                        kids["equivalent_taken"] = child["equivalent"]
                        kids["equivalent_taken_display"] = qty_display(
                            Fraction(child["equivalent"])
                        )
                        children.append(kids)
                return {
                    **base,
                    "produced_by_op": producer["op_id"] if producer else None,
                    "children": children,
                }

            lineage = node(row["id"], None)
            lineage.pop("amount_taken", None)
            lineage.pop("amount_taken_display", None)

            used_rows = conn.execute(
                "SELECT i.op_id, i.amount, i.equivalent, i.position, o.kind, o.created_at "
                "FROM op_inputs i JOIN operations o ON o.id = i.op_id "
                "WHERE i.tube_id = ? ORDER BY o.id",
                (row["id"],),
            ).fetchall()
            used_in = []
            for use in used_rows:
                outs = conn.execute(
                    "SELECT o.amount, o.equivalent, t.code FROM op_outputs o "
                    "JOIN tubes t ON t.id = o.tube_id "
                    "WHERE o.op_id = ? ORDER BY o.position",
                    (use["op_id"],),
                ).fetchall()
                used_in.append(
                    {
                        "op_id": use["op_id"],
                        "kind": use["kind"],
                        "created_at": use["created_at"],
                        "amount": use["amount"],
                        "amount_display": qty_display(Fraction(use["amount"])) if use["amount"] else None,
                        "equivalent": use["equivalent"],
                        "equivalent_display": qty_display(Fraction(use["equivalent"]))
                        if use["equivalent"] else None,
                        "outputs": [
                            {
                                "code": x["code"],
                                "amount": x["amount"],
                                "amount_display": qty_display(Fraction(x["amount"]))
                                if x["amount"]
                                else None,
                                "equivalent": x["equivalent"],
                                "equivalent_display": qty_display(Fraction(x["equivalent"]))
                                if x["equivalent"]
                                else None,
                            }
                            for x in outs
                        ],
                    }
                )
            retired_rows = conn.execute(
                "SELECT r.op_id, r.discarded, r.discarded_equivalent, r.form, o.created_at "
                "FROM retire_items r JOIN operations o ON o.id = r.op_id WHERE r.tube_id = ?",
                (row["id"],),
            ).fetchall()
            retired_at = [
                {
                    "op_id": x["op_id"],
                    "created_at": x["created_at"],
                    "form": x["form"],
                    "discarded": x["discarded"],
                    "discarded_display": qty_display(Fraction(x["discarded"]))
                    if x["discarded"]
                    else None,
                    "discarded_equivalent": x["discarded_equivalent"],
                    "discarded_equivalent_display": qty_display(
                        Fraction(x["discarded_equivalent"])
                    )
                    if x["discarded_equivalent"]
                    else None,
                }
                for x in retired_rows
            ]
            processing = [
                {
                    "op_id": p["op_id"],
                    "action": p["action"],
                    "created_at": p["created_at"],
                    "form_before": p["form_before"],
                    "volume_before": p["volume_before"],
                    "volume_before_display": qty_display(Fraction(p["volume_before"])),
                    "equivalent_before": p["equivalent_before"],
                    "equivalent_before_display": qty_display(
                        Fraction(p["equivalent_before"])
                    ),
                    "form_after": p["form_after"],
                    "volume_after": p["volume_after"],
                    "volume_after_display": qty_display(Fraction(p["volume_after"])),
                    "equivalent_after": p["equivalent_after"],
                    "equivalent_after_display": qty_display(
                        Fraction(p["equivalent_after"])
                    ),
                }
                for p in conn.execute(
                    "SELECT p.*, o.created_at FROM process_items p "
                    "JOIN operations o ON o.id = p.op_id "
                    "WHERE p.tube_id = ? ORDER BY p.op_id, p.position",
                    (row["id"],),
                ).fetchall()
            ]
            return {
                "seq": latest if seq is None else seq,
                "latest_seq": latest,
                "tube": {**self._snapshot_item(item), "composition": composition},
                "composition": composition,
                "lineage": lineage,
                "used_in": used_in,
                "retired_at": retired_at,
                "processing": processing,
            }

    def batch_trace(self, batch: str, seq: int | None = None) -> dict[str, Any]:
        batch = parse_batch(batch)
        with self._read() as conn:
            latest = self._latest_seq(conn)
            if seq is not None:
                seq = int(seq)
                if seq < 0 or seq > latest:
                    raise NotFoundError(f"操作序号 {seq} 不存在")
            intake_rows = conn.execute(
                "SELECT * FROM tubes WHERE kind = 'intake' AND batch = ? COLLATE NOCASE "
                "ORDER BY id",
                (batch,),
            ).fetchall()
            if not intake_rows:
                raise NotFoundError(f"没有找到批次为 {batch} 的来样")
            intake_ids = {row["id"] for row in intake_rows}
            snapshot = self._snapshot(conn, seq)
            by_id = {v["id"]: v for v in snapshot.values()}
            batch_label = intake_rows[0]["batch"]

            touched: list[dict[str, Any]] = []
            for item in sorted(snapshot.values(), key=lambda x: x["id"]):
                share = sum(
                    (
                        Fraction(comp["share"])
                        for comp in item["composition"]
                        if comp["intake_id"] in intake_ids
                    ),
                    Fraction(0),
                )
                if share <= 0:
                    continue
                batch_initial = share * item["initial_fraction"]
                batch_remaining = share * item["remaining_fraction"]
                batch_eq_initial = share * item["initial_equivalent_fraction"]
                batch_eq_remaining = share * item["remaining_equivalent_fraction"]
                touched.append(
                    {
                        **self._snapshot_item(item),
                        "batch_share": qty_str(share),
                        "batch_share_display": qty_display(share),
                        "batch_share_percent": percent_display(share),
                        "batch_amount_initial": qty_str(batch_initial),
                        "batch_amount_initial_display": qty_display(batch_initial),
                        "batch_amount_remaining": qty_str(batch_remaining),
                        "batch_amount_remaining_display": qty_display(batch_remaining),
                        "batch_equivalent_initial": qty_str(batch_eq_initial),
                        "batch_equivalent_initial_display": qty_display(batch_eq_initial),
                        "batch_equivalent_remaining": qty_str(batch_eq_remaining),
                        "batch_equivalent_remaining_display": qty_display(batch_eq_remaining),
                        "is_intake_of_batch": item["id"] in intake_ids,
                    }
                )
            touched_ids = {item["id"] for item in touched}

            flow = []
            if touched_ids:
                placeholders = ",".join("?" for _ in touched_ids)
                op_ids = set()
                for sql in (
                    f"SELECT DISTINCT op_id FROM op_inputs WHERE tube_id IN ({placeholders})",
                    f"SELECT DISTINCT op_id FROM op_outputs WHERE tube_id IN ({placeholders})",
                    f"SELECT DISTINCT op_id FROM retire_items WHERE tube_id IN ({placeholders})",
                    f"SELECT DISTINCT op_id FROM process_items WHERE tube_id IN ({placeholders})",
                ):
                    for row in conn.execute(sql, tuple(touched_ids)).fetchall():
                        op_ids.add(row["op_id"])
                for op_id in sorted(op_ids):
                    if seq is not None and op_id > seq:
                        continue
                    detail = self._op_detail(conn, op_id)
                    detail["inputs"] = [
                        {
                            **inp,
                            "contains_batch": inp["tube_id"] in touched_ids,
                        }
                        for inp in detail["inputs"]
                    ]
                    detail["outputs"] = [
                        {
                            **out,
                            "contains_batch": out["tube_id"] in touched_ids,
                        }
                        for out in detail["outputs"]
                    ]
                    detail["processing"] = [
                        {
                            **proc,
                            "contains_batch": proc["tube_id"] in touched_ids,
                        }
                        for proc in detail["processing"]
                    ]
                    flow.append(detail)

            return {
                "seq": latest if seq is None else seq,
                "latest_seq": latest,
                "batch": batch_label,
                "intake_tubes": [
                    {
                        **self._snapshot_item(by_id[row["id"]]),
                        "composition": self._composition_amounts(by_id[row["id"]]),
                    }
                    for row in intake_rows
                    if row["id"] in by_id
                ],
                "tubes": touched,
                "flow": flow,
            }

    @staticmethod
    def _recall_affected(
        conn: sqlite3.Connection,
        recall_id: int,
        by_id: dict[int, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        items = conn.execute(
            "SELECT tube_id, code, remaining, equivalent, form, composition, position "
            "FROM recall_items WHERE recall_id = ? ORDER BY position",
            (recall_id,),
        ).fetchall()
        affected = []
        for item in items:
            current = by_id.get(item["tube_id"])
            affected.append(
                {
                    "tube_id": item["tube_id"],
                    "code": item["code"],
                    "form": item["form"],
                    "remaining": item["remaining"],
                    "remaining_display": qty_display(Fraction(item["remaining"])),
                    "equivalent": item["equivalent"],
                    "equivalent_display": qty_display(Fraction(item["equivalent"])),
                    "composition": json.loads(item["composition"]),
                    "current_status": current["status"] if current else "unknown",
                    "current_form": current["form"] if current else None,
                    "current_remaining": current["remaining"] if current else None,
                    "current_remaining_display": current["remaining_display"]
                    if current
                    else None,
                    "current_equivalent": current["remaining_equivalent"]
                    if current
                    else None,
                    "current_equivalent_display": current["remaining_equivalent_display"]
                    if current
                    else None,
                }
            )
        return affected

    def recalls(self) -> dict[str, Any]:
        with self._read() as conn:
            snapshot = self._snapshot(conn)
            by_id = {v["id"]: v for v in snapshot.values()}
            rows = conn.execute(
                "SELECT * FROM recalls ORDER BY id"
            ).fetchall()
            recalls_out = []
            for row in rows:
                affected = self._recall_affected(conn, row["id"], by_id)
                recalls_out.append(
                    {
                        "id": row["id"],
                        "op_id": row["op_id"],
                        "seq": row["op_id"],
                        "batch": row["batch"],
                        "note": row["note"],
                        "created_at": row["created_at"],
                        "affected_count": len(affected),
                        "affected": affected,
                    }
                )
            return {"recalls": recalls_out}

    def recall_detail(self, recall_id: int) -> dict[str, Any]:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM recalls WHERE id = ?", (recall_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"召回记录 {recall_id} 不存在")
            snapshot = self._snapshot(conn)
            by_id = {v["id"]: v for v in snapshot.values()}
            affected = self._recall_affected(conn, recall_id, by_id)
            return {
                "id": row["id"],
                "op_id": row["op_id"],
                "seq": row["op_id"],
                "batch": row["batch"],
                "note": row["note"],
                "created_at": row["created_at"],
                "affected_count": len(affected),
                "affected": affected,
            }

    # -- writes -------------------------------------------------------------

    def register_intake(
        self, code: Any, batch: Any, volume: Any, note: Any = ""
    ) -> dict[str, Any]:
        code = parse_code(code, "来样管编号")
        batch = parse_batch(batch)
        volume = parse_qty(volume, "来样量")
        note = parse_note(note)

        with self._transaction() as conn:
            if self._tube_by_code(conn, code) is not None:
                raise ConflictError(f"编号 {code} 已存在，来样登记未提交")
            self._fault("intake-before-insert")
            cur = conn.execute(
                "INSERT INTO operations(kind, note, created_at) VALUES ('intake', ?, ?)",
                (note, now_text()),
            )
            op_id = int(cur.lastrowid)
            self._fault("intake-after-op")
            cur = conn.execute(
                "INSERT INTO tubes(code, kind, batch, initial_volume, initial_equivalent, "
                "composition, created_op, created_at, note) "
                "VALUES (?, 'intake', ?, ?, ?, ?, ?, ?, ?)",
                (
                    code,
                    batch,
                    qty_str(volume),
                    qty_str(volume),
                    json.dumps({}),
                    op_id,
                    now_text(),
                    note,
                ),
            )
            tube_id = int(cur.lastrowid)
            conn.execute(
                "UPDATE tubes SET composition = ? WHERE id = ?",
                (json.dumps({str(tube_id): "1"}), tube_id),
            )
            self._fault("intake-after-tube")
            conn.execute(
                "INSERT INTO op_outputs(op_id, tube_id, amount, equivalent, position) "
                "VALUES (?, ?, ?, ?, 0)",
                (op_id, tube_id, qty_str(volume), qty_str(volume)),
            )
            conn.execute(
                "INSERT INTO tube_moves(op_id, tube_id, kind, volume, equivalent, position) "
                "VALUES (?, ?, 'create', ?, ?, 0)",
                (op_id, tube_id, qty_str(volume), qty_str(volume)),
            )
            self._fault("intake-after-output")
            return self._op_detail(conn, op_id)

    def split(
        self,
        inputs: Any,
        outputs: Any,
        note: Any = "",
    ) -> dict[str, Any]:
        """一次操作从多管取样、往多管分装/混合。

        所有校验在写库前完成；任何一条不合格，整次操作回滚。
        """
        note = parse_note(note)
        if not isinstance(inputs, list) or not inputs:
            raise LedgerError("至少需要一管取样来源")
        if not isinstance(outputs, list) or not outputs:
            raise LedgerError("至少需要一支分装成品")

        problems: list[str] = []
        parsed_inputs: list[dict[str, Any]] = []
        seen_inputs: dict[str, int] = {}
        for idx, entry in enumerate(inputs, start=1):
            if not isinstance(entry, dict):
                problems.append(f"第 {idx} 条取样记录格式不正确")
                continue
            try:
                code = parse_code(entry.get("tube", entry.get("code")), f"第 {idx} 条取样管编号")
                amount = parse_qty(entry.get("amount"), f"第 {idx} 条取样量")
            except LedgerError as exc:
                problems.append(str(exc.message))
                continue
            key = code.casefold()
            if key in seen_inputs:
                problems.append(f"取样管 {code} 在本次操作中重复出现")
                continue
            seen_inputs[key] = idx
            parsed_inputs.append({"code": code, "amount": amount, "index": idx})

        parsed_outputs: list[dict[str, Any]] = []
        seen_outputs: dict[str, int] = {}
        for idx, entry in enumerate(outputs, start=1):
            if not isinstance(entry, dict):
                problems.append(f"第 {idx} 条分装记录格式不正确")
                continue
            try:
                code = parse_code(entry.get("code", entry.get("tube")), f"第 {idx} 条成品管编号")
                amount = parse_qty(entry.get("amount"), f"第 {idx} 条分装量")
            except LedgerError as exc:
                problems.append(str(exc.message))
                continue
            key = code.casefold()
            if key in seen_outputs:
                problems.append(f"成品管 {code} 在本次操作中重复出现")
                continue
            if key in seen_inputs:
                problems.append(f"成品管编号 {code} 与取样管编号相同")
                continue
            seen_outputs[key] = idx
            parsed_outputs.append({"code": code, "amount": amount, "index": idx})

        if problems:
            raise LedgerError("操作未提交，请检查以下问题", problems)

        total_in = sum((entry["amount"] for entry in parsed_inputs), Fraction(0))
        total_out = sum((entry["amount"] for entry in parsed_outputs), Fraction(0))
        if total_in != total_out:
            raise LedgerError(
                "取样总量与分装总量不一致，操作未提交",
                [
                    f"取样合计 {qty_display(total_in)} µL（精确值 {qty_str(total_in)}）",
                    f"分装合计 {qty_display(total_out)} µL（精确值 {qty_str(total_out)}）",
                    f"差额 {qty_display(total_in - total_out)} µL",
                ],
            )

        with self._transaction() as conn:
            tube_rows: dict[str, sqlite3.Row] = {}
            for entry in parsed_inputs:
                row = self._tube_by_code(conn, entry["code"])
                if row is None:
                    problems.append(f"取样管 {entry['code']} 不存在")
                    continue
                tube_rows[entry["code"].casefold()] = row

            for entry in parsed_outputs:
                if self._tube_by_code(conn, entry["code"]) is not None:
                    problems.append(f"成品管编号 {entry['code']} 已存在")

            snapshot = self._snapshot(conn)
            for entry in parsed_inputs:
                row = tube_rows.get(entry["code"].casefold())
                if row is None:
                    continue
                item = snapshot[row["id"]]
                if item["status"] == "retired":
                    problems.append(f"取样管 {entry['code']} 已报废，不能取样")
                elif item["status"] == "exhausted":
                    problems.append(f"取样管 {entry['code']} 已用尽，不能取样")
                elif item["is_dry"]:
                    problems.append(
                        f"取样管 {entry['code']} 是冻干干样，干样不能直接移取；"
                        f"请先用空白溶剂复溶，再按实测体积取样"
                    )
                else:
                    remaining = item["remaining_fraction"]
                    if entry["amount"] > remaining:
                        problems.append(
                            f"取样管 {entry['code']} 用量不足：需要 "
                            f"{qty_display(entry['amount'])} µL，仅剩 "
                            f"{qty_display(remaining)} µL"
                        )
                if item["recalled"]:
                    batches = "、".join(sorted({r["batch"] for r in item["recalls"]}))
                    problems.append(
                        f"取样管 {entry['code']} 处于召回状态（批次 {batches}），禁止取样"
                    )

            if problems:
                raise LedgerError("操作未提交，请检查以下问题", problems)

            # 每支来源管这次取走的原样折算量 = 取用比例 × 该管当前剩余折算量。
            # 从干样复溶后的管里取样也走这条路径，按实测体积占比带走折算量。
            for entry in parsed_inputs:
                row = tube_rows[entry["code"].casefold()]
                item = snapshot[row["id"]]
                entry["equivalent"] = (
                    entry["amount"] / item["remaining_fraction"]
                ) * item["remaining_equivalent_fraction"]

            # 计算每支成品的组成：完全混匀后按取样比例摊到各来源。
            comp: dict[int, Fraction] = defaultdict(Fraction)
            for entry in parsed_inputs:
                row = tube_rows[entry["code"].casefold()]
                source_comp = json.loads(row["composition"])
                source_share = entry["amount"] / total_in
                for intake_id, share in source_comp.items():
                    comp[int(intake_id)] += source_share * Fraction(share)

            self._fault("split-before-insert")
            cur = conn.execute(
                "INSERT INTO operations(kind, note, created_at) VALUES ('split', ?, ?)",
                (note, now_text()),
            )
            op_id = int(cur.lastrowid)
            self._fault("split-after-op")

            for pos, entry in enumerate(parsed_inputs):
                row = tube_rows[entry["code"].casefold()]
                conn.execute(
                    "INSERT INTO op_inputs(op_id, tube_id, amount, equivalent, position) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        op_id,
                        row["id"],
                        qty_str(entry["amount"]),
                        qty_str(entry["equivalent"]),
                        pos,
                    ),
                )
                conn.execute(
                    "INSERT INTO tube_moves(op_id, tube_id, kind, volume, equivalent, position) "
                    "VALUES (?, ?, 'take', ?, ?, ?)",
                    (
                        op_id,
                        row["id"],
                        qty_str(-entry["amount"]),
                        qty_str(-entry["equivalent"]),
                        pos,
                    ),
                )
                self._fault("split-input", pos)

            total_equivalent_in = sum(
                (entry["equivalent"] for entry in parsed_inputs), Fraction(0)
            )
            comp_json = json.dumps(_sort_composition(comp))
            for pos, entry in enumerate(parsed_outputs):
                # 成品按分装量占比继承本次取走的全部原样折算量，保证折算量守恒。
                out_equivalent = (
                    entry["amount"] / total_out
                ) * total_equivalent_in
                cur = conn.execute(
                    "INSERT INTO tubes(code, kind, batch, initial_volume, "
                    "initial_equivalent, composition, created_op, created_at, note) "
                    "VALUES (?, 'pooled', NULL, ?, ?, ?, ?, ?, '')",
                    (
                        entry["code"],
                        qty_str(entry["amount"]),
                        qty_str(out_equivalent),
                        comp_json,
                        op_id,
                        now_text(),
                    ),
                )
                tube_id = int(cur.lastrowid)
                conn.execute(
                    "INSERT INTO op_outputs(op_id, tube_id, amount, equivalent, position) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (op_id, tube_id, qty_str(entry["amount"]), qty_str(out_equivalent), pos),
                )
                conn.execute(
                    "INSERT INTO tube_moves(op_id, tube_id, kind, volume, equivalent, position) "
                    "VALUES (?, ?, 'create', ?, ?, ?)",
                    (op_id, tube_id, qty_str(entry["amount"]), qty_str(out_equivalent), pos),
                )
                self._fault("split-output", pos)

            return self._op_detail(conn, op_id)

    def retire(self, tubes: Any, note: Any = "") -> dict[str, Any]:
        note = parse_note(note)
        if not isinstance(tubes, list) or not tubes:
            raise LedgerError("至少需要选择一管报废")
        codes: list[str] = []
        seen: set[str] = set()
        problems: list[str] = []
        for idx, entry in enumerate(tubes, start=1):
            raw = entry.get("tube", entry.get("code")) if isinstance(entry, dict) else entry
            try:
                code = parse_code(raw, f"第 {idx} 条报废管编号")
            except LedgerError as exc:
                problems.append(str(exc.message))
                continue
            key = code.casefold()
            if key in seen:
                problems.append(f"报废管 {code} 重复出现")
                continue
            seen.add(key)
            codes.append(code)
        if problems:
            raise LedgerError("报废未提交，请检查以下问题", problems)

        with self._transaction() as conn:
            snapshot = self._snapshot(conn)
            rows: list[sqlite3.Row] = []
            for code in codes:
                row = self._tube_by_code(conn, code)
                if row is None:
                    problems.append(f"管 {code} 不存在")
                    continue
                if snapshot[row["id"]]["status"] == "retired":
                    problems.append(f"管 {code} 已经报废")
                    continue
                rows.append(row)
            if problems:
                raise LedgerError("报废未提交，请检查以下问题", problems)

            cur = conn.execute(
                "INSERT INTO operations(kind, note, created_at) VALUES ('retire', ?, ?)",
                (note, now_text()),
            )
            op_id = int(cur.lastrowid)
            self._fault("retire-after-op")
            for pos, row in enumerate(rows):
                item = snapshot[row["id"]]
                discarded = item["remaining_fraction"]
                discarded_eq = item["remaining_equivalent_fraction"]
                conn.execute(
                    "INSERT INTO retire_items(op_id, tube_id, discarded, "
                    "discarded_equivalent, form, position) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        op_id,
                        row["id"],
                        qty_str(max(discarded, Fraction(0))),
                        qty_str(max(discarded_eq, Fraction(0))),
                        item["form"],
                        pos,
                    ),
                )
                self._fault("retire-item", pos)
            return self._op_detail(conn, op_id)

    def recall(self, batch: Any, note: Any = "") -> dict[str, Any]:
        batch = parse_batch(batch)
        note = parse_note(note)
        with self._transaction() as conn:
            intake_rows = conn.execute(
                "SELECT * FROM tubes WHERE kind = 'intake' AND batch = ? COLLATE NOCASE "
                "ORDER BY id",
                (batch,),
            ).fetchall()
            if not intake_rows:
                raise NotFoundError(f"没有找到批次为 {batch} 的来样，召回未提交")
            intake_ids = {row["id"] for row in intake_rows}
            snapshot = self._snapshot(conn)
            by_id = {v["id"]: v for v in snapshot.values()}
            batch_label = intake_rows[0]["batch"]

            affected: list[dict[str, Any]] = []
            for item in sorted(snapshot.values(), key=lambda x: x["id"]):
                if item["status"] != "available":
                    continue
                share = sum(
                    (
                        Fraction(comp["share"])
                        for comp in item["composition"]
                        if comp["intake_id"] in intake_ids
                    ),
                    Fraction(0),
                )
                if share <= 0:
                    continue
                affected.append(item)

            cur = conn.execute(
                "INSERT INTO operations(kind, note, created_at) VALUES ('recall', ?, ?)",
                (note, now_text()),
            )
            op_id = int(cur.lastrowid)
            cur = conn.execute(
                "INSERT INTO recalls(op_id, batch, note, created_at) VALUES (?, ?, ?, ?)",
                (op_id, batch_label, note, now_text()),
            )
            recall_id = int(cur.lastrowid)
            self._fault("recall-after-header")
            for pos, item in enumerate(affected):
                conn.execute(
                    "INSERT INTO recall_items(recall_id, tube_id, code, remaining, "
                    "equivalent, form, composition, position) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        recall_id,
                        item["id"],
                        item["code"],
                        item["remaining"],
                        item["remaining_equivalent"],
                        item["form"],
                        json.dumps(item["composition"], ensure_ascii=False),
                        pos,
                    ),
                )
                self._fault("recall-item", pos)
            self._fault("recall-after-items")
            detail = self._op_detail(conn, op_id)
            return {
                "operation": detail,
                "recall": {
                    "id": recall_id,
                    "batch": batch_label,
                    "note": note,
                    "affected_count": len(affected),
                    "affected": [
                        {
                            "tube_id": item["id"],
                            "code": item["code"],
                            "form": item["form"],
                            "remaining": item["remaining"],
                            "remaining_display": item["remaining_display"],
                            "equivalent": item["remaining_equivalent"],
                            "equivalent_display": item["remaining_equivalent_display"],
                            "composition": item["composition"],
                        }
                        for item in affected
                    ],
                },
            }

    # -- 冻干 / 复溶 ---------------------------------------------------------

    def lyophilize(self, tubes: Any, note: Any = "") -> dict[str, Any]:
        """把液体管冻干成干样。

        冻干只去掉溶剂：体积账归零（不再是可移取的液体），但原样折算量原样保留，
        干样仍在库、仍可追溯、仍受召回约束，只是不能直接取样。
        """
        return self._process(tubes, note, action="dry")

    def reconstitute(self, tubes: Any, note: Any = "") -> dict[str, Any]:
        """用空白溶剂复溶干样，按实测体积记账。

        复溶只增加体积这本账；原样折算量保持不变（不因加了空白溶剂而变化），
        后续取样按实测体积占比带走折算量。
        """
        return self._process(tubes, note, action="reconstitute")

    def _process(self, tubes: Any, note: Any, action: str) -> dict[str, Any]:
        note = parse_note(note)
        if not isinstance(tubes, list) or not tubes:
            raise LedgerError(
                "至少需要选择一管" + ("冻干" if action == "dry" else "复溶")
            )
        problems: list[str] = []
        parsed: list[dict[str, Any]] = []
        seen: set[str] = set()
        for idx, entry in enumerate(tubes, start=1):
            if isinstance(entry, dict):
                raw_code = entry.get("tube", entry.get("code"))
                raw_amount = entry.get("volume")
            else:
                raw_code, raw_amount = entry, None
            label = "冻干" if action == "dry" else "复溶"
            try:
                code = parse_code(raw_code, f"第 {idx} 条{label}管编号")
            except LedgerError as exc:
                problems.append(str(exc.message))
                continue
            key = code.casefold()
            if key in seen:
                problems.append(f"{label}管 {code} 重复出现")
                continue
            seen.add(key)
            amount = None
            if action == "reconstitute":
                try:
                    amount = parse_qty(raw_amount, f"第 {idx} 条复溶实测体积")
                except LedgerError as exc:
                    problems.append(str(exc.message))
                    continue
            parsed.append({"code": code, "volume": amount, "index": idx})
        if problems:
            raise LedgerError(
                ("冻干" if action == "dry" else "复溶") + "未提交，请检查以下问题",
                problems,
            )

        label = "冻干" if action == "dry" else "复溶"
        with self._transaction() as conn:
            snapshot = self._snapshot(conn)
            rows: list[tuple[sqlite3.Row, dict[str, Any], Fraction]] = []
            for entry in parsed:
                row = self._tube_by_code(conn, entry["code"])
                if row is None:
                    problems.append(f"管 {entry['code']} 不存在")
                    continue
                item = snapshot[row["id"]]
                if item["status"] == "retired":
                    problems.append(f"管 {entry['code']} 已报废，不能{label}")
                    continue
                if item["status"] == "exhausted":
                    problems.append(f"管 {entry['code']} 已用尽，不能{label}")
                    continue
                if action == "dry":
                    if item["is_dry"]:
                        problems.append(f"管 {entry['code']} 已经是干样，无需再次冻干")
                        continue
                else:
                    if not item["is_dry"]:
                        problems.append(
                            f"管 {entry['code']} 不是干样（当前为液体），无需复溶"
                        )
                        continue
                if item["recalled"]:
                    batches = "、".join(sorted({r["batch"] for r in item["recalls"]}))
                    problems.append(
                        f"管 {entry['code']} 处于召回状态（批次 {batches}），禁止{label}"
                    )
                    continue
                volume_after = (
                    Fraction(0) if action == "dry" else entry["volume"]
                )
                rows.append((row, item, volume_after))
            if problems:
                raise LedgerError(f"{label}未提交，请检查以下问题", problems)

            cur = conn.execute(
                "INSERT INTO operations(kind, note, created_at) VALUES (?, ?, ?)",
                ("lyophilize" if action == "dry" else "reconstitute", note, now_text()),
            )
            op_id = int(cur.lastrowid)
            self._fault("process-after-op")
            for pos, (row, item, volume_after) in enumerate(rows):
                equivalent = item["remaining_equivalent_fraction"]
                if action == "dry":
                    form_after = "dry"
                    volume_after_text = "0"
                    # 冻干去掉溶剂：体积结存归零，折痕量不动。
                    volume_delta = -item["remaining_fraction"]
                else:
                    form_after = "liquid"
                    volume_after_text = qty_str(volume_after)
                    # 复溶按实测体积入账，折痕量不动。
                    volume_delta = volume_after
                conn.execute(
                    "INSERT INTO process_items(op_id, tube_id, position, action, "
                    "form_before, volume_before, equivalent_before, form_after, "
                    "volume_after, equivalent_after) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        op_id,
                        row["id"],
                        pos,
                        action,
                        item["form"],
                        item["remaining"],
                        item["remaining_equivalent"],
                        form_after,
                        volume_after_text,
                        qty_str(equivalent),
                    ),
                )
                conn.execute(
                    "INSERT INTO tube_moves(op_id, tube_id, kind, volume, equivalent, position) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        op_id,
                        row["id"],
                        action,
                        qty_str(volume_delta),
                        "0",
                        pos,
                    ),
                )
                self._fault("process-item", pos)
            return self._op_detail(conn, op_id)

    # -- exports ------------------------------------------------------------

    def inventory_csv(self, seq: int | None = None) -> str:
        data = self.inventory(seq)
        lines = [
            "编号,类型,批次,物态,状态,是否召回,初始量(uL),剩余量(uL),"
            "初始原样折算量(uL),原样折算量剩余(uL),组成,创建操作号,创建时间,备注"
        ]
        for item in data["tubes"]:
            comp = _composition_text(item["composition"])
            status_label = {
                "available": "可用",
                "exhausted": "已用尽",
                "retired": "已报废",
            }.get(item["status"], item["status"])
            kind_label = "来样" if item["kind"] == "intake" else "分装/混合"
            recalled = "是" if item["recalled"] else "否"
            form_label = "干样" if item["is_dry"] else "液体"
            fields = [
                item["code"],
                kind_label,
                item["batch"] or "",
                form_label,
                status_label,
                recalled,
                item["initial_volume_display"],
                item["remaining_display"],
                item["initial_equivalent_display"],
                item["remaining_equivalent_display"],
                comp,
                str(item["created_op"]),
                item["created_at"],
                item["note"],
            ]
            lines.append(",".join(self._csv_field(f) for f in fields))
        return "﻿" + "\r\n".join(lines) + "\r\n"

    def recalls_csv(self) -> str:
        data = self.recalls()["recalls"]
        lines = [
            "召回号,操作序号,批次,发起时间,备注,受影响管,当时物态,当时剩余量(uL),"
            "当时原样折算量(uL),当时组成,当前状态"
        ]
        for recall in data:
            if not recall["affected"]:
                lines.append(
                    ",".join(
                        self._csv_field(f)
                        for f in [
                            recall["id"],
                            recall["seq"],
                            recall["batch"],
                            recall["created_at"],
                            recall["note"],
                            "（无仍在库的受影响管）",
                            "",
                            "",
                            "",
                            "",
                            "",
                        ]
                    )
                )
                continue
            for item in recall["affected"]:
                lines.append(
                    ",".join(
                        self._csv_field(f)
                        for f in [
                            recall["id"],
                            recall["seq"],
                            recall["batch"],
                            recall["created_at"],
                            recall["note"],
                            item["code"],
                            "干样" if item["form"] == "dry" else "液体",
                            item["remaining_display"],
                            item["equivalent_display"],
                            _composition_text(item["composition"]),
                            item["current_status"],
                        ]
                    )
                )
        return "﻿" + "\r\n".join(lines) + "\r\n"

    @staticmethod
    def _csv_field(value: Any) -> str:
        text = "" if value is None else str(value)
        if any(ch in text for ch in ',"\r\n'):
            return '"' + text.replace('"', '""') + '"'
        return text

    def suggest_codes(self, count: int = 3, prefix: str = "S") -> list[str]:
        count = max(1, min(int(count), 20))
        prefix = "".join(ch for ch in prefix if ch.isalnum() or ch in "-_") or "S"
        stamp = datetime.now().strftime("%Y%m%d")
        with self._read() as conn:
            existing = {
                row["code"].casefold()
                for row in conn.execute("SELECT code FROM tubes").fetchall()
            }
        result = []
        n = 1
        while len(result) < count:
            candidate = f"{prefix}-{stamp}-{n:03d}"
            if candidate.casefold() not in existing:
                result.append(candidate)
                existing.add(candidate.casefold())
            n += 1
            if n > 100000:
                break
        return result
