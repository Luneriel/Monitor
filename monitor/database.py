"""SQLite 持久化层。

需求文档要求的表结构：
    programs(id, name, path, icon_path, added_time, last_run_time)
    logs(id, program_id, start_time, end_time)

为了支持"追踪开关""删除但保留日志""多实例独立计时"等需求，额外增加了
    programs.tracking / programs.visible / programs.keep_logs / logs.pid
字段，必需字段保持原样。

日志一律写入本地文件 ``data/monitor.db``，不驻留内存。
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import threading
import time
from typing import Any, Iterable, Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS programs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    path          TEXT    NOT NULL UNIQUE,
    icon_path     TEXT,
    added_time    REAL    NOT NULL,
    last_run_time REAL,
    tracking      INTEGER NOT NULL DEFAULT 1,
    visible       INTEGER NOT NULL DEFAULT 1,
    keep_logs     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    program_id INTEGER NOT NULL,
    pid        INTEGER,
    start_time REAL    NOT NULL,
    end_time   REAL,
    FOREIGN KEY (program_id) REFERENCES programs (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_logs_program ON logs (program_id, start_time DESC);
CREATE INDEX IF NOT EXISTS idx_logs_start   ON logs (start_time DESC);
CREATE INDEX IF NOT EXISTS idx_logs_open    ON logs (end_time);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- "控制"页：每个程序各一条规则（数值 + 单位：s / m / h）
CREATE TABLE IF NOT EXISTS control_rules (
    program_id     INTEGER PRIMARY KEY,
    daily_enabled  INTEGER NOT NULL DEFAULT 0,
    daily_value    INTEGER NOT NULL DEFAULT 0,
    daily_unit     TEXT    NOT NULL DEFAULT 'm',
    streak_enabled INTEGER NOT NULL DEFAULT 0,
    streak_value   INTEGER NOT NULL DEFAULT 0,
    streak_unit    TEXT    NOT NULL DEFAULT 'm',
    pause_value    INTEGER NOT NULL DEFAULT 0,
    pause_unit     TEXT    NOT NULL DEFAULT 'm',
    window_enabled INTEGER NOT NULL DEFAULT 0,
    windows        TEXT    NOT NULL DEFAULT '[]',
    FOREIGN KEY (program_id) REFERENCES programs (id) ON DELETE CASCADE
);

-- "控制"页的运行时状态（每日累计 / 连续累计 / 休息累计 / 暂停与临时解锁截止时间）
CREATE TABLE IF NOT EXISTS control_state (
    program_id     INTEGER PRIMARY KEY,
    day_key        TEXT    NOT NULL DEFAULT '',
    used_seconds   REAL    NOT NULL DEFAULT 0,
    streak_seconds REAL    NOT NULL DEFAULT 0,
    rest_seconds   REAL    NOT NULL DEFAULT 0,
    pause_until    REAL    NOT NULL DEFAULT 0,
    unlock_until   REAL    NOT NULL DEFAULT 0,
    limit_logged   TEXT    NOT NULL DEFAULT '',
    pause_logged   INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (program_id) REFERENCES programs (id) ON DELETE CASCADE
);
"""

# logs 表新增列（老库需要迁移）
LOG_COLUMNS = (
    ("kind", "TEXT NOT NULL DEFAULT 'run'"),
    ("message", "TEXT"),
)

# programs 表新增列（老库需要迁移）
# kind: 'program' = 普通程序（按 exe 路径追踪）；'site' = 网站（path 存域名匹配串）
PROGRAM_COLUMNS = (
    ("kind", "TEXT NOT NULL DEFAULT 'program'"),
)

# control_rules 表新增列（老库需要迁移）
# window_enabled: 是否启用"指定时间段"；windows: JSON [[起, 止, 星期掩码], ...]
CONTROL_RULE_COLUMNS = (
    ("window_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("windows", "TEXT NOT NULL DEFAULT '[]'"),
)


def windows_json(windows: Any) -> str:
    """``Window`` 列表（或 ``[起, 止, 天]`` 行）-> 数据库里的紧凑 JSON。"""
    rows: list[list[int]] = []
    for window in windows or ():
        to_row = getattr(window, "to_row", None)
        if callable(to_row):
            rows.append([int(v) for v in to_row()])
            continue
        try:
            rows.append([int(window[0]), int(window[1]), int(window[2])])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    return json.dumps(rows, separators=(",", ":"))


def normalize_path(path: str) -> str:
    """路径归一化，用于比对同一个可执行文件。"""
    if not path:
        return ""
    return os.path.normcase(os.path.abspath(path)).rstrip("\\/") or path


def day_bounds(day: str) -> tuple[Optional[float], Optional[float]]:
    """本地日期 ``"YYYY-MM-DD"`` -> ``[当天 00:00, 次日 00:00)`` 的时间戳。

    日志页按"天"取某页的记录时用它换算区间：区间比较走 ``start_time`` 索引，
    不必对每一行做 ``date()`` 计算。
    """
    try:
        year, month, dom = (int(part) for part in str(day).split("-"))
        start = time.mktime((year, month, dom, 0, 0, 0, 0, 0, -1))
        nxt = datetime.date(year, month, dom) + datetime.timedelta(days=1)
        end = time.mktime((nxt.year, nxt.month, nxt.day, 0, 0, 0, 0, 0, -1))
    except (TypeError, ValueError, OverflowError):
        return None, None
    return float(start), float(end)


class Database:
    """线程安全的 SQLite 封装（监控线程与 UI 线程共用）。"""

    def __init__(self, path: str = config.DB_PATH) -> None:
        config.ensure_dirs()
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            # 保持"单文件 monitor.db"：使用默认日志模式，不产生 -wal/-shm 附文件
            self._conn.execute("PRAGMA journal_mode=DELETE")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.commit()
        self._migrate()
        self._repair_dangling_sessions()

    def _migrate(self) -> None:
        """老库补列（logs.kind / logs.message；programs.kind；control_rules 的单位列）。"""
        with self._lock:
            existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(logs)")}
            for name, ddl in LOG_COLUMNS:
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE logs ADD COLUMN {name} {ddl}")

            # programs：老库没有 kind 列，补上后默认全部当作"程序"
            program_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(programs)")}
            for name, ddl in PROGRAM_COLUMNS:
                if name not in program_columns:
                    self._conn.execute(f"ALTER TABLE programs ADD COLUMN {name} {ddl}")

            # control_rules：老库用 *_minutes，现在改成"数值 + 单位"两列
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(control_rules)")}
            for old, new in (("daily_minutes", "daily_value"),
                             ("streak_minutes", "streak_value"),
                             ("pause_minutes", "pause_value")):
                if old in columns and new not in columns:
                    # 老库的数值按分钟理解，单位列默认 'm'
                    self._conn.execute(f"ALTER TABLE control_rules RENAME COLUMN {old} TO {new}")
                    columns.discard(old)
                    columns.add(new)
            for name in ("daily_unit", "streak_unit", "pause_unit"):
                if name not in columns:
                    self._conn.execute(
                        f"ALTER TABLE control_rules ADD COLUMN {name} TEXT NOT NULL DEFAULT 'm'")
            # control_rules：新增"指定时间段"两列（老库补默认值）
            for name, ddl in CONTROL_RULE_COLUMNS:
                if name not in columns:
                    self._conn.execute(f"ALTER TABLE control_rules ADD COLUMN {name} {ddl}")
            self._conn.commit()

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
                self._conn.close()
            except Exception:
                pass

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def _repair_dangling_sessions(self) -> None:
        """上次异常退出遗留的未闭合日志：直接以开始时间闭合，避免虚假超长记录。"""
        self._exec("UPDATE logs SET end_time = start_time WHERE end_time IS NULL")

    # ------------------------------------------------------------------
    # programs
    # ------------------------------------------------------------------
    def add_program(self, path: str, name: str = "", icon_path: Optional[str] = None,
                    tracking: bool = True) -> Optional[int]:
        """新增追踪程序。

        * 同路径且**仍在列表中** -> 返回 None（视为重复）；
        * 同路径但曾被删除（``visible = 0``，例如"删除但保留日志"）-> **重新启用**该行，
          这样删掉的程序可以再次添加回来，历史日志也仍然挂在同一个程序上。
        """
        norm = normalize_path(path)
        if not norm:
            return None
        if not name:
            name = os.path.splitext(os.path.basename(norm))[0]
        existing = self.find_program_by_path(norm)
        if existing is not None:
            if bool(existing["visible"]):
                return None
            program_id = int(existing["id"])
            self._exec(
                "UPDATE programs SET name = ?, icon_path = COALESCE(?, icon_path),"
                " visible = 1, keep_logs = 0, tracking = ? WHERE id = ?",
                (name, icon_path, 1 if tracking else 0, program_id),
            )
            return program_id
        cur = self._exec(
            "INSERT INTO programs (name, path, icon_path, added_time, last_run_time,"
            " tracking, visible, keep_logs) VALUES (?, ?, ?, ?, NULL, ?, 1, 1)",
            (name, norm, icon_path, time.time(), 1 if tracking else 0),
        )
        return int(cur.lastrowid)

    def find_program_by_path(self, path: str) -> Optional[sqlite3.Row]:
        norm = normalize_path(path)
        rows = self._query("SELECT * FROM programs WHERE path = ?", (norm,))
        return rows[0] if rows else None

    def add_site(self, pattern: str, name: str = "") -> Optional[int]:
        """新增追踪网站。

        ``pattern`` 是用户填写的域名/网址匹配串（内部会归一化成
        ``normalize_pattern()`` 的形式存进 ``programs.path``，并标记 ``kind = 'site'``）。
        这是**唯一**把 ``path`` 当域名而不是文件路径来解释的地方。
        """
        from .site_watch import display_name, normalize_pattern

        norm = normalize_pattern(pattern)
        if not norm:
            return None
        if not name:
            name = display_name(norm)
        rows = self._query("SELECT * FROM programs WHERE kind = 'site' AND path = ?", (norm,))
        if rows:
            row = rows[0]
            if bool(row["visible"]):
                return None
            program_id = int(row["id"])
            self._exec(
                "UPDATE programs SET name = ?, visible = 1, keep_logs = 0, tracking = 1 WHERE id = ?",
                (name, program_id),
            )
            return program_id
        cur = self._exec(
            "INSERT INTO programs (name, path, icon_path, added_time, last_run_time,"
            " tracking, visible, keep_logs, kind) VALUES (?, ?, NULL, ?, NULL, 1, 1, 1, 'site')",
            (name, norm, time.time()),
        )
        return int(cur.lastrowid)

    def get_program(self, program_id: int) -> Optional[sqlite3.Row]:
        rows = self._query("SELECT * FROM programs WHERE id = ?", (program_id,))
        return rows[0] if rows else None

    def programs(self, visible_only: bool = False, include_deleted: bool = True,
                 kind: Optional[str] = None) -> list[sqlite3.Row]:
        """返回程序列表。

        * visible_only=True  -> 首页列表（未被删除的）
        * include_deleted    -> 是否包含"删除但保留日志"的程序（日志页下拉菜单需要）
        * kind               -> 'program' / 'site' 过滤；None = 两者都要
        """
        sql = "SELECT * FROM programs"
        clauses = []
        if visible_only:
            clauses.append("visible = 1")
        elif not include_deleted:
            clauses.append("visible = 1")
        if kind:
            clauses.append(f"kind = '{kind}'")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY added_time ASC, id ASC"
        return self._query(sql)

    def sites(self, visible_only: bool = False, include_deleted: bool = True) -> list[sqlite3.Row]:
        """追踪的网站列表（``programs.kind = 'site'``）。"""
        return self.programs(visible_only=visible_only, include_deleted=include_deleted, kind="site")

    def rename_program(self, program_id: int, name: str) -> None:
        name = (name or "").strip()
        if not name:
            return
        self._exec("UPDATE programs SET name = ? WHERE id = ?", (name, program_id))

    def set_icon_path(self, program_id: int, icon_path: str) -> None:
        self._exec("UPDATE programs SET icon_path = ? WHERE id = ?", (icon_path, program_id))

    def set_tracking(self, program_id: int, enabled: bool) -> None:
        self._exec("UPDATE programs SET tracking = ? WHERE id = ?", (1 if enabled else 0, program_id))

    def touch_last_run(self, program_id: int, ts: Optional[float] = None) -> None:
        self._exec("UPDATE programs SET last_run_time = ? WHERE id = ?", (ts or time.time(), program_id))

    def remove_programs(self, program_ids: Iterable[int], keep_logs: bool = True) -> list[str]:
        """执行删除；返回被彻底删除（连日志一起）的程序名称列表。

        无论是否保留日志，都一并清掉该程序的控制规则 / 运行时状态 / 独立通知设置
        （需求：程序被删除后不再保存它的独立设置）。
        """
        purged: list[str] = []
        for pid in program_ids:
            row = self.get_program(pid)
            if row is None:
                continue
            self.delete_control_rules(int(pid))
            self.set_setting(self.notify_key(int(pid)), "")
            if keep_logs:
                # 从首页消失，但保留日志，仍出现在日志页"按程序分类"下拉菜单
                self._exec(
                    "UPDATE programs SET visible = 0, tracking = 0, keep_logs = 1 WHERE id = ?",
                    (pid,),
                )
            else:
                purged.append(row["name"])
                self._exec("DELETE FROM logs WHERE program_id = ?", (pid,))
                self._exec("DELETE FROM programs WHERE id = ?", (pid,))
        return purged

    # ------------------------------------------------------------------
    # 控制：规则与运行时状态
    # ------------------------------------------------------------------
    def set_control_rules(self, program_id: int, *, daily_enabled: bool = False,
                          daily_value: int = 0, daily_unit: str = "m",
                          streak_enabled: bool = False, streak_value: int = 0,
                          streak_unit: str = "m", pause_value: int = 0,
                          pause_unit: str = "m", window_enabled: bool = False,
                          windows: Any = None) -> None:
        self._exec(
            "INSERT INTO control_rules (program_id, daily_enabled, daily_value, daily_unit,"
            " streak_enabled, streak_value, streak_unit, pause_value, pause_unit,"
            " window_enabled, windows)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(program_id) DO UPDATE SET daily_enabled = excluded.daily_enabled,"
            " daily_value = excluded.daily_value, daily_unit = excluded.daily_unit,"
            " streak_enabled = excluded.streak_enabled, streak_value = excluded.streak_value,"
            " streak_unit = excluded.streak_unit, pause_value = excluded.pause_value,"
            " pause_unit = excluded.pause_unit,"
            " window_enabled = excluded.window_enabled, windows = excluded.windows",
            (program_id, 1 if daily_enabled else 0, int(daily_value), str(daily_unit or "m"),
             1 if streak_enabled else 0, int(streak_value), str(streak_unit or "m"),
             int(pause_value), str(pause_unit or "m"),
             1 if window_enabled else 0, windows_json(windows)),
        )

    def control_rules(self, program_id: int) -> Optional[sqlite3.Row]:
        rows = self._query("SELECT * FROM control_rules WHERE program_id = ?", (program_id,))
        return rows[0] if rows else None

    def all_control_rules(self) -> dict[int, dict]:
        return {int(r["program_id"]): dict(r)
                for r in self._query("SELECT * FROM control_rules")}

    def delete_control_rules(self, program_id: int) -> None:
        self._exec("DELETE FROM control_rules WHERE program_id = ?", (program_id,))
        self._exec("DELETE FROM control_state WHERE program_id = ?", (program_id,))

    def control_state(self, program_id: int) -> Optional[sqlite3.Row]:
        rows = self._query("SELECT * FROM control_state WHERE program_id = ?", (program_id,))
        return rows[0] if rows else None

    def all_control_state(self) -> dict[int, dict]:
        return {int(r["program_id"]): dict(r)
                for r in self._query("SELECT * FROM control_state")}

    def save_control_state(self, program_id: int, **fields: Any) -> None:
        self.save_control_states({program_id: fields})

    def save_control_states(self, states: dict[int, dict]) -> None:
        """原子批量保存；任一写入失败时整批回滚。"""
        if not states:
            return
        with self._lock, self._conn:
            for program_id, fields in states.items():
                if not fields:
                    continue
                names = list(fields)
                columns = ", ".join(names)
                placeholders = ", ".join("?" for _ in names)
                updates = ", ".join(f"{name} = excluded.{name}" for name in names)
                self._conn.execute(
                    f"INSERT INTO control_state (program_id, {columns}) VALUES (?, {placeholders}) "
                    f"ON CONFLICT(program_id) DO UPDATE SET {updates}",
                    (program_id, *(fields[name] for name in names)))

    # ------------------------------------------------------------------
    # 通知设置（JSON 存进 settings 表，作用域 = global 或某个程序）
    # ------------------------------------------------------------------
    @staticmethod
    def notify_key(program_id: Optional[int]) -> str:
        return "notify.global" if program_id is None else f"notify.program.{int(program_id)}"

    def get_notify_settings(self, program_id: Optional[int] = None) -> dict:
        raw = self.get_setting(self.notify_key(program_id), "")
        if not raw:
            return {}
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}

    def set_notify_settings(self, settings: dict, program_id: Optional[int] = None) -> None:
        self.set_setting(self.notify_key(program_id), json.dumps(settings, ensure_ascii=False))

    def all_notify_program_ids(self) -> list[int]:
        rows = self._query("SELECT key, value FROM settings WHERE key LIKE 'notify.program.%'")
        ids = []
        for row in rows:
            if not str(row["value"] or "").strip():
                continue                     # 已被移除（值为空）的独立设置不再列出
            try:
                ids.append(int(str(row["key"]).rsplit(".", 1)[1]))
            except (TypeError, ValueError):
                continue
        return ids

    def delete_setting(self, key: str) -> None:
        self._exec("DELETE FROM settings WHERE key = ?", (key,))

    # ------------------------------------------------------------------
    # 控制类日志（kind='control'，message 为提示文字）
    # ------------------------------------------------------------------
    def add_control_log(self, program_id: int, message: str, kind: str = "control",
                        ts: Optional[float] = None) -> int:
        now = ts or time.time()
        cur = self._exec(
            "INSERT INTO logs (program_id, pid, start_time, end_time, kind, message)"
            " VALUES (?, NULL, ?, ?, ?, ?)",
            (program_id, now, now, kind, message),
        )
        return int(cur.lastrowid)

    # ------------------------------------------------------------------
    # logs
    # ------------------------------------------------------------------
    def open_log(self, program_id: int, pid: Optional[int], start_ts: float) -> int:
        cur = self._exec(
            "INSERT INTO logs (program_id, pid, start_time, end_time) VALUES (?, ?, ?, NULL)",
            (program_id, pid, start_ts),
        )
        return int(cur.lastrowid)

    def close_log(self, log_id: int, end_ts: float) -> None:
        self._exec("UPDATE logs SET end_time = ? WHERE id = ? AND end_time IS NULL", (end_ts, log_id))

    def close_logs_for_pid(self, program_id: int, pid: int, end_ts: float) -> None:
        self._exec(
            "UPDATE logs SET end_time = ? WHERE program_id = ? AND pid = ? AND end_time IS NULL",
            (end_ts, program_id, pid),
        )

    def close_all_open(self, end_ts: Optional[float] = None) -> None:
        ts = end_ts or time.time()
        self._exec("UPDATE logs SET end_time = ? WHERE end_time IS NULL", (ts,))

    def delete_logs(self, log_ids: Iterable[int]) -> int:
        """按 id 删除日志记录，返回实际删除条数。"""
        ids = [int(i) for i in log_ids]
        if not ids:
            return 0
        removed = 0
        for chunk_start in range(0, len(ids), 400):
            chunk = ids[chunk_start:chunk_start + 400]
            placeholders = ",".join("?" * len(chunk))
            cursor = self._exec(f"DELETE FROM logs WHERE id IN ({placeholders})", chunk)
            removed += max(0, cursor.rowcount)
        return removed

    def count_logs(self, program_id: Optional[int] = None) -> int:
        if program_id is None:
            rows = self._query("SELECT COUNT(*) AS n FROM logs")
        else:
            rows = self._query("SELECT COUNT(*) AS n FROM logs WHERE program_id = ?", (program_id,))
        return int(rows[0]["n"]) if rows else 0

    def logs_for_program(self, program_id: int, limit: int = 2000,
                         since: Optional[float] = None,
                         until: Optional[float] = None,
                         offset: int = 0) -> list[sqlite3.Row]:
        clauses = ["program_id = ?"]
        params: list[Any] = [program_id]
        if since is not None:
            clauses.append("start_time >= ?")
            params.append(float(since))
        if until is not None:
            clauses.append("start_time < ?")
            params.append(float(until))
        params.extend([int(limit), int(offset)])
        return self._query(
            "SELECT * FROM logs WHERE " + " AND ".join(clauses)
            + " ORDER BY start_time DESC LIMIT ? OFFSET ?",
            params,
        )

    def all_logs(self, limit: int = 3000, since: Optional[float] = None,
                 until: Optional[float] = None) -> list[sqlite3.Row]:
        """混合日志（按时间倒序），附带程序名称 / 路径 / 图标 / 类型。"""
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("l.start_time >= ?")
            params.append(float(since))
        if until is not None:
            clauses.append("l.start_time < ?")
            params.append(float(until))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        return self._query(
            "SELECT l.*, p.name AS program_name, p.path AS program_path,"
            " p.icon_path AS icon_path, p.kind AS program_kind"
            " FROM logs l LEFT JOIN programs p ON p.id = l.program_id"
            + where +
            " ORDER BY l.start_time DESC LIMIT ?",
            params,
        )

    # ------------------------------------------------------------------
    # 日志分页：只在需要时读回"这一页"的记录，不把整张表取进内存
    # ------------------------------------------------------------------
    @staticmethod
    def _log_range_clauses(since: Optional[float],
                           until: Optional[float]) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("l.start_time >= ?")
            params.append(float(since))
        if until is not None:
            clauses.append("l.start_time < ?")
            params.append(float(until))
        return clauses, params

    def log_day_counts(self, since: Optional[float] = None,
                       until: Optional[float] = None) -> list[tuple[str, int, Optional[float]]]:
        """每天的 ``(本地日期, 条数, 当天最晚的结束时间)``，按日期倒序。

        只回一天一行，日志再多也只读回几十行 —— 日志页用它算分页边界，
        顺便当作"数据有没有变"的指纹（条数或最晚结束时间一变就重建列表）。
        """
        clauses, params = self._log_range_clauses(since, until)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._query(
            "SELECT date(l.start_time, 'unixepoch', 'localtime') AS day,"
            " COUNT(*) AS n, MAX(l.end_time) AS last_end"
            " FROM logs l" + where + " GROUP BY day ORDER BY day DESC",
            params,
        )
        result: list[tuple[str, int, Optional[float]]] = []
        for row in rows:
            day = str(row["day"] or "")
            if not day:
                continue
            last_end = row["last_end"]
            result.append((day, int(row["n"]), float(last_end) if last_end else None))
        return result

    def logs_in_day(self, day: str, limit: int, offset: int = 0) -> list[sqlite3.Row]:
        """某一天（本地日期）的日志，按时间倒序；``offset`` 供"单日强制分页"切片。"""
        low, high = day_bounds(day)
        if low is None or high is None:
            return []
        return self._query(
            "SELECT l.*, p.name AS program_name, p.path AS program_path,"
            " p.icon_path AS icon_path, p.kind AS program_kind"
            " FROM logs l LEFT JOIN programs p ON p.id = l.program_id"
            " WHERE l.start_time >= ? AND l.start_time < ?"
            " ORDER BY l.start_time DESC LIMIT ? OFFSET ?",
            (low, high, int(limit), int(offset)),
        )

    def logs_page(self, since: Optional[float] = None, until: Optional[float] = None,
                  offset: int = 0, limit: int = 200) -> list[sqlite3.Row]:
        """按时间倒序取一段日志（"按程序分类"以外的平铺分页用）。"""
        clauses, params = self._log_range_clauses(since, until)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params = list(params) + [int(limit), int(offset)]
        return self._query(
            "SELECT l.*, p.name AS program_name, p.path AS program_path,"
            " p.icon_path AS icon_path, p.kind AS program_kind"
            " FROM logs l LEFT JOIN programs p ON p.id = l.program_id"
            + where + " ORDER BY l.start_time DESC LIMIT ? OFFSET ?",
            params,
        )

    def logs_snapshot(self, program_id: Optional[int] = None, since: Optional[float] = None,
                      until: Optional[float] = None) -> tuple[int, Optional[float]]:
        """``(条数, 最晚结束时间)``：判断日志数据有没有变化，一条聚合 SQL。"""
        clauses, params = self._log_range_clauses(since, until)
        if program_id is not None:
            clauses = ["l.program_id = ?"] + clauses
            params = [int(program_id)] + list(params)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._query(
            "SELECT COUNT(*) AS n, MAX(l.end_time) AS last_end FROM logs l" + where, params)
        if not rows:
            return 0, None
        last_end = rows[0]["last_end"]
        return int(rows[0]["n"]), (float(last_end) if last_end else None)

    def logs_by_ids(self, ids: list) -> list[sqlite3.Row]:
        """按 id 批量取日志（删除确认弹窗要列出**跨页**选中的全部记录）。"""
        wanted = [int(value) for value in (ids or ())]
        if not wanted:
            return []
        marks = ",".join("?" for _ in wanted)
        return self._query(
            "SELECT l.*, p.name AS program_name, p.path AS program_path,"
            " p.icon_path AS icon_path, p.kind AS program_kind"
            " FROM logs l LEFT JOIN programs p ON p.id = l.program_id"
            " WHERE l.id IN (" + marks + ") ORDER BY l.start_time DESC",
            wanted,
        )

    def log_date_parts(self) -> list[tuple[int, int, int]]:
        """返回日志中出现过的 (年, 月, 日) 组合（本地时间），供"按时间分类"筛选。"""
        rows = self._query("SELECT DISTINCT start_time FROM logs")
        seen: set[tuple[int, int, int]] = set()
        for row in rows:
            try:
                local = time.localtime(float(row["start_time"]))
            except (TypeError, ValueError):
                continue
            seen.add((local.tm_year, local.tm_mon, local.tm_mday))
        return sorted(seen, reverse=True)

    def last_end_time(self, program_id: int) -> Optional[float]:
        rows = self._query(
            "SELECT MAX(end_time) AS t FROM logs WHERE program_id = ? AND end_time IS NOT NULL",
            (program_id,),
        )
        return rows[0]["t"] if rows and rows[0]["t"] is not None else None

    def last_end_times(self) -> dict[int, float]:
        rows = self._query(
            "SELECT program_id, MAX(end_time) AS t FROM logs WHERE end_time IS NOT NULL"
            " GROUP BY program_id"
        )
        return {int(r["program_id"]): float(r["t"]) for r in rows if r["t"] is not None}

    def active_logs(self) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM logs WHERE end_time IS NULL")

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------
    def get_setting(self, key: str, default: str = "") -> str:
        rows = self._query("SELECT value FROM settings WHERE key = ?", (key,))
        if not rows:
            return default
        value = rows[0]["value"]
        return default if value is None else str(value)

    def get_bool(self, key: str, default: bool = False) -> bool:
        return self.get_setting(key, "1" if default else "0") == "1"

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get_setting(key, str(default)))
        except (TypeError, ValueError):
            return default

    def set_setting(self, key: str, value: Any) -> None:
        if isinstance(value, bool):
            value = "1" if value else "0"
        self._exec(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    # ------------------------------------------------------------------
    # 卡片顺序（拖动排序）：id 列表以 JSON 存在 settings 表里
    #
    # 说明：不用 programs.sort_order 列，因为首页列表也读 programs()，改列会连带
    # 改动首页顺序；这里每个页面 / 每个分类各存一份顺序，互不影响。没被列出的
    # id 由界面按数据库顺序追加在后面（新增程序自然排在最后）。
    # ------------------------------------------------------------------
    def get_order(self, key: str) -> list[int]:
        raw = self.get_setting(key, "")
        if not raw:
            return []
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(value, list):
            return []
        result: list[int] = []
        for item in value:
            try:
                result.append(int(item))
            except (TypeError, ValueError):
                continue
        return result

    def set_order(self, key: str, ids: Iterable[int]) -> None:
        ordered: list[int] = []
        seen: set[int] = set()
        for item in ids:
            try:
                value = int(item)
            except (TypeError, ValueError):
                continue
            if value in seen:
                continue
            seen.add(value)
            ordered.append(value)
        self.set_setting(key, json.dumps(ordered, separators=(",", ":")))
