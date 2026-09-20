#!/usr/bin/env python3
"""Small, crash-tolerant persistent-agent registry for Polyphony.

This module deliberately contains the business logic used by both the CLI and
the MCP adapter.  It is a registry/message bus, not a scheduler or daemon.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid
from typing import Any, Iterable


SCHEMA_VERSION = 2
MAX_MESSAGE_CHARS = 8_000
DEFAULT_LEASE_SECONDS = 3600
DEFAULT_MAX_HOPS = 2
DEFAULT_MAX_FANOUT = 4


def now() -> float:
    return time.time()


def default_db_path() -> Path:
    configured = os.environ.get("POLYPHONY_RUNTIME_DB")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if root:
            return Path(root) / "Polyphony" / "runtime.db"
    root = os.environ.get("XDG_STATE_HOME")
    if root:
        return Path(root) / "polyphony" / "runtime.db"
    return Path.home() / ".polyphony" / "runtime.db"


def canonical_workspace(workspace: str | os.PathLike[str]) -> str:
    raw = os.fspath(workspace)
    if os.name == "nt":
        match = re.match(r"^/([A-Za-z])(?:/(.*))?$", raw)
        if match:
            tail = (match.group(2) or "").replace("/", "\\")
            raw = match.group(1).upper() + ":\\" + tail
    return str(Path(raw).expanduser().resolve(strict=False))


def workspace_key(workspace: str | os.PathLike[str]) -> str:
    normalized = os.path.normcase(canonical_workspace(workspace))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


class RuntimeErrorBase(RuntimeError):
    pass


class Busy(RuntimeErrorBase):
    pass


class NotFound(RuntimeErrorBase):
    pass


class Runtime:
    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = Path(path).expanduser() if path else default_db_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA foreign_keys = ON")
        if str(self.path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.init_db()

    def close(self) -> None:
        self.conn.close()

    def init_db(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY,
                parent_agent_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                workspace TEXT NOT NULL,
                conversation_id TEXT,
                status TEXT NOT NULL CHECK(status IN ('active','idle','waiting','dead')),
                current_task_id TEXT,
                created_at REAL NOT NULL,
                last_used_at REAL NOT NULL,
                last_seen_at REAL NOT NULL,
                lease_expires_at REAL,
                conversation_generation INTEGER NOT NULL DEFAULT 1,
                checkpoint TEXT
            );
            CREATE INDEX IF NOT EXISTS agents_reuse_idx
              ON agents(parent_agent_id, workspace_id, status, last_used_at);
            CREATE UNIQUE INDEX IF NOT EXISTS agents_conversation_idx
              ON agents(conversation_id) WHERE conversation_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS agent_conversations (
                agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                account_id TEXT NOT NULL,
                conversation_id TEXT,
                conversation_generation INTEGER NOT NULL DEFAULT 1,
                checkpoint TEXT,
                last_used_at REAL NOT NULL,
                PRIMARY KEY(agent_id, account_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS account_conversation_owner_idx
              ON agent_conversations(conversation_id) WHERE conversation_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL REFERENCES agents(id),
                parent_task_id TEXT,
                account_id TEXT,
                workspace_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed','cancelled')),
                summary TEXT NOT NULL,
                hop_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                started_at REAL,
                completed_at REAL,
                heartbeat_at REAL,
                lease_expires_at REAL,
                lease_token TEXT,
                attempts INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS tasks_agent_idx ON tasks(agent_id, status);
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                task_id TEXT,
                from_agent TEXT NOT NULL,
                to_agent TEXT NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('message','request','response','handoff')),
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                read_at REAL
            );
            CREATE INDEX IF NOT EXISTS messages_inbox_idx
              ON messages(workspace_id, to_agent, read_at, created_at);
            """
        )
        row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self.conn.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
        else:
            version = int(row[0])
            if version == 1:
                columns = {item[1] for item in self.conn.execute("PRAGMA table_info(tasks)")}
                if "account_id" not in columns:
                    self.conn.execute("ALTER TABLE tasks ADD COLUMN account_id TEXT")
                self.conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
            elif version != SCHEMA_VERSION:
                raise RuntimeErrorBase(f"unsupported runtime schema {row[0]} (expected {SCHEMA_VERSION})")

    def _recover_stale_locked(self, at: float) -> int:
        stale = self.conn.execute(
            "SELECT id, agent_id, account_id, summary FROM tasks WHERE status IN ('pending','running') "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at < ?", (at,)
        ).fetchall()
        for row in stale:
            self.conn.execute(
                "UPDATE tasks SET status='failed', completed_at=?, lease_token=NULL WHERE id=? "
                "AND status IN ('pending','running')", (at, row["id"])
            )
            checkpoint = f"Previous task timed out or crashed: {row['summary']}"
            if row["account_id"]:
                mapped = self.conn.execute(
                    "SELECT conversation_generation FROM agent_conversations WHERE agent_id=? AND account_id=?",
                    (row["agent_id"], row["account_id"]),
                ).fetchone()
                generation = int(mapped[0]) + 1 if mapped else 2
                self.conn.execute(
                    "INSERT INTO agent_conversations(agent_id,account_id,conversation_id,conversation_generation,checkpoint,last_used_at) "
                    "VALUES(?,?,NULL,?,?,?) ON CONFLICT(agent_id,account_id) DO UPDATE SET "
                    "conversation_id=NULL, conversation_generation=excluded.conversation_generation, "
                    "checkpoint=excluded.checkpoint, last_used_at=excluded.last_used_at",
                    (row["agent_id"], row["account_id"], generation, checkpoint, at),
                )
                self.conn.execute(
                    "UPDATE agents SET status='idle', current_task_id=NULL, lease_expires_at=NULL, last_seen_at=? "
                    "WHERE id=? AND current_task_id=?", (at, row["agent_id"], row["id"])
                )
            else:
                self.conn.execute(
                    "UPDATE agents SET status='idle', current_task_id=NULL, lease_expires_at=NULL, "
                    "conversation_id=NULL, conversation_generation=conversation_generation + 1, "
                    "checkpoint=?, last_seen_at=? "
                    "WHERE id=? AND current_task_id=?", (checkpoint, at, row["agent_id"], row["id"])
                )
        return len(stale)

    def recover_stale(self) -> int:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            count = self._recover_stale_locked(now())
            self.conn.execute("COMMIT")
            return count
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def claim_task(
        self,
        parent_agent_id: str,
        workspace: str,
        summary: str,
        *,
        agent_id: str | None = None,
        parent_task_id: str | None = None,
        fresh_agent: bool = False,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        max_hops: int = DEFAULT_MAX_HOPS,
        max_fanout: int = DEFAULT_MAX_FANOUT,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        if not parent_agent_id.strip():
            raise ValueError("parent_agent_id is required")
        if not summary.strip():
            raise ValueError("summary is required")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        ws = canonical_workspace(workspace)
        wid = workspace_key(ws)
        at = now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._recover_stale_locked(at)
            hop = 0
            if parent_task_id:
                parent = self.conn.execute(
                    "SELECT t.workspace_id, t.hop_count, t.agent_id, t.status, a.parent_agent_id "
                    "FROM tasks t JOIN agents a ON a.id = t.agent_id WHERE t.id=?",
                    (parent_task_id,),
                ).fetchone()
                if not parent or parent["workspace_id"] != wid:
                    raise NotFound("parent task is missing or belongs to another workspace")
                if parent["agent_id"] != parent_agent_id and parent["parent_agent_id"] != parent_agent_id:
                    raise NotFound("parent task is not owned by this parent")
                if parent["status"] not in ("pending", "running"):
                    raise Busy(f"parent task is not pending or running (status: {parent['status']})")
                hop = int(parent["hop_count"]) + 1
            if hop > max_hops:
                raise Busy(f"delegation hop limit reached ({max_hops})")
            if parent_task_id:
                children = self.conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE parent_task_id=? AND status IN ('pending','running')",
                    (parent_task_id,),
                ).fetchone()[0]
                if children >= max_fanout:
                    raise Busy(f"delegation fan-out limit reached ({max_fanout})")

            selected = None
            if agent_id:
                selected = self.conn.execute(
                    "SELECT * FROM agents WHERE id=? AND parent_agent_id=? AND workspace_id=?",
                    (agent_id, parent_agent_id, wid),
                ).fetchone()
                if not selected:
                    raise NotFound("agent is not owned by this parent in this workspace")
            elif not fresh_agent:
                selected = self.conn.execute(
                    "SELECT * FROM agents WHERE parent_agent_id=? AND workspace_id=? AND status='idle' "
                    "AND (lease_expires_at IS NULL OR lease_expires_at < ?) ORDER BY last_used_at DESC LIMIT 1",
                    (parent_agent_id, wid, at),
                ).fetchone()
            if selected and selected["status"] == "dead":
                raise Busy("agent is dead")
            if selected and selected["status"] in ("active", "waiting"):
                raise Busy("agent already has an active task")
            if selected and selected["current_task_id"]:
                raise Busy("agent already has an active task")
            if selected and selected["lease_expires_at"] is not None and selected["lease_expires_at"] > at:
                raise Busy("agent lease is still active")
            if not selected:
                selected_id = "agent-" + uuid.uuid4().hex
                self.conn.execute(
                    "INSERT INTO agents(id,parent_agent_id,workspace_id,workspace,status,created_at,last_used_at,last_seen_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (selected_id, parent_agent_id, wid, ws, "idle", at, at, at),
                )
                selected = self.conn.execute("SELECT * FROM agents WHERE id=?", (selected_id,)).fetchone()

            conversation_id = selected["conversation_id"]
            conversation_generation = selected["conversation_generation"]
            checkpoint = selected["checkpoint"]
            if account_id:
                mapped = self.conn.execute(
                    "SELECT * FROM agent_conversations WHERE agent_id=? AND account_id=?",
                    (selected["id"], account_id),
                ).fetchone()
                if not mapped and selected["conversation_id"]:
                    # Idempotent lazy migration: the first known saved account adopts
                    # the legacy conversation; later accounts always get their own.
                    any_mapping = self.conn.execute(
                        "SELECT 1 FROM agent_conversations WHERE agent_id=? LIMIT 1", (selected["id"],)
                    ).fetchone()
                    if not any_mapping:
                        self.conn.execute(
                            "INSERT INTO agent_conversations(agent_id,account_id,conversation_id,conversation_generation,checkpoint,last_used_at) "
                            "VALUES(?,?,?,?,?,?)",
                            (selected["id"], account_id, selected["conversation_id"], selected["conversation_generation"], selected["checkpoint"], at),
                        )
                        self.conn.execute(
                            "UPDATE agents SET conversation_id=NULL, checkpoint=NULL WHERE id=?", (selected["id"],)
                        )
                        mapped = self.conn.execute(
                            "SELECT * FROM agent_conversations WHERE agent_id=? AND account_id=?",
                            (selected["id"], account_id),
                        ).fetchone()
                if mapped:
                    conversation_id = mapped["conversation_id"]
                    conversation_generation = mapped["conversation_generation"]
                    checkpoint = mapped["checkpoint"]
                else:
                    conversation_id = None
                    conversation_generation = 1
                    checkpoint = None

            task_id = "task-" + uuid.uuid4().hex
            token = uuid.uuid4().hex
            expiry = at + lease_seconds
            self.conn.execute(
                "INSERT INTO tasks(id,agent_id,parent_task_id,account_id,workspace_id,status,summary,hop_count,created_at,started_at,heartbeat_at,lease_expires_at,lease_token,attempts) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (task_id, selected["id"], parent_task_id, account_id, wid, "running", summary[:2_000], hop, at, at, at, expiry, token),
            )
            self.conn.execute(
                "UPDATE agents SET status='active', current_task_id=?, last_used_at=?, last_seen_at=?, lease_expires_at=? WHERE id=?",
                (task_id, at, at, expiry, selected["id"]),
            )
            self.conn.execute("COMMIT")
            return {
                "task_id": task_id,
                "agent_id": selected["id"],
                "parent_agent_id": parent_agent_id,
                "workspace_id": wid,
                "account_id": account_id,
                "conversation_id": conversation_id,
                "conversation_generation": conversation_generation,
                "checkpoint": checkpoint,
                "lease_token": token,
                "hop_count": hop,
            }
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def heartbeat(self, task_id: str, lease_token: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> bool:
        at = now()
        cur = self.conn.execute(
            "UPDATE tasks SET heartbeat_at=?, lease_expires_at=? WHERE id=? AND lease_token=? AND status='running'",
            (at, at + lease_seconds, task_id, lease_token),
        )
        if cur.rowcount:
            self.conn.execute("UPDATE agents SET last_seen_at=?, lease_expires_at=? WHERE current_task_id=?", (at, at + lease_seconds, task_id))
        return bool(cur.rowcount)

    def finish_task(self, task_id: str, lease_token: str, status: str = "completed") -> bool:
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("invalid terminal task status")
        at = now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT agent_id FROM tasks WHERE id=? AND lease_token=? AND status='running'", (task_id, lease_token)).fetchone()
            if not row:
                self.conn.execute("ROLLBACK")
                return False
            self.conn.execute("UPDATE tasks SET status=?, completed_at=?, lease_token=NULL, lease_expires_at=NULL WHERE id=?", (status, at, task_id))
            self.conn.execute(
                "UPDATE agents SET status='idle', current_task_id=NULL, lease_expires_at=NULL, last_seen_at=?, last_used_at=? "
                "WHERE id=? AND current_task_id=?",
                (at, at, row["agent_id"], task_id),
            )
            self.conn.execute("COMMIT")
            return True
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def handoff_task(self, task_id: str, from_agent_id: str, to_agent_id: str, lease_token: str, lease_seconds: int = DEFAULT_LEASE_SECONDS, max_hops: int = DEFAULT_MAX_HOPS) -> dict[str, Any]:
        """Transfer one live task to an idle sibling owned by the same parent."""
        if lease_seconds <= 0 or max_hops < 0:
            raise ValueError("lease_seconds must be positive and max_hops cannot be negative")
        at = now()
        new_token = uuid.uuid4().hex
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._recover_stale_locked(at)
            row = self.conn.execute(
                "SELECT t.*, a.parent_agent_id FROM tasks t JOIN agents a ON a.id=t.agent_id "
                "WHERE t.id=? AND t.agent_id=? AND t.lease_token=? AND t.status='running'",
                (task_id, from_agent_id, lease_token),
            ).fetchone()
            target = self.conn.execute("SELECT * FROM agents WHERE id=? AND status='idle' AND current_task_id IS NULL", (to_agent_id,)).fetchone()
            if not row or not target or target["parent_agent_id"] != row["parent_agent_id"] or target["workspace_id"] != row["workspace_id"]:
                raise Busy("handoff requires the valid task lease and an idle sibling of the same parent/workspace")
            hop = int(row["hop_count"]) + 1
            if hop > max_hops:
                raise Busy(f"handoff hop limit reached ({max_hops})")
            self.conn.execute(
                "UPDATE tasks SET agent_id=?, lease_token=?, heartbeat_at=?, lease_expires_at=?, hop_count=? WHERE id=? AND status='running'",
                (to_agent_id, new_token, at, at + lease_seconds, hop, task_id),
            )
            self.conn.execute("UPDATE agents SET status='idle', current_task_id=NULL, lease_expires_at=NULL, last_seen_at=? WHERE id=?", (at, from_agent_id))
            self.conn.execute("UPDATE agents SET status='active', current_task_id=?, lease_expires_at=?, last_seen_at=?, last_used_at=? WHERE id=?", (task_id, at + lease_seconds, at, at, to_agent_id))
            self.conn.execute("COMMIT")
            return {"task_id": task_id, "from_agent_id": from_agent_id, "to_agent_id": to_agent_id, "lease_token": new_token}
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def set_conversation(self, agent_id: str, parent_agent_id: str, workspace: str, conversation_id: str | None, account_id: str | None = None) -> dict[str, Any]:
        wsid = workspace_key(workspace)
        if conversation_id is not None:
            conversation_id = conversation_id.strip() or None
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            target = self.conn.execute(
                "SELECT * FROM agents WHERE id=? AND parent_agent_id=? AND workspace_id=?",
                (agent_id, parent_agent_id, wsid),
            ).fetchone()
            if not target:
                raise NotFound("agent is not owned by this parent in this workspace")
            if target["status"] == "dead":
                raise Busy("agent is dead")
            if account_id:
                if conversation_id:
                    owner = self.conn.execute(
                        "SELECT agent_id, account_id FROM agent_conversations WHERE conversation_id=? "
                        "AND NOT (agent_id=? AND account_id=?)",
                        (conversation_id, agent_id, account_id),
                    ).fetchone()
                    legacy_owner = self.conn.execute(
                        "SELECT id FROM agents WHERE conversation_id=? AND id != ?", (conversation_id, agent_id)
                    ).fetchone()
                    if owner or legacy_owner:
                        raise Busy("conversation is already owned by another agent or account")
                generation = self.conn.execute(
                    "SELECT conversation_generation FROM agent_conversations WHERE agent_id=? AND account_id=?",
                    (agent_id, account_id),
                ).fetchone()
                self.conn.execute(
                    "INSERT INTO agent_conversations(agent_id,account_id,conversation_id,conversation_generation,checkpoint,last_used_at) "
                    "VALUES(?,?,?,?,NULL,?) ON CONFLICT(agent_id,account_id) DO UPDATE SET "
                    "conversation_id=excluded.conversation_id, checkpoint=NULL, last_used_at=excluded.last_used_at",
                    (agent_id, account_id, conversation_id, int(generation[0]) if generation else 1, now()),
                )
                result = dict(self.conn.execute(
                    "SELECT * FROM agent_conversations WHERE agent_id=? AND account_id=?", (agent_id, account_id)
                ).fetchone())
                self.conn.execute("COMMIT")
                return result
            if conversation_id:
                owner = self.conn.execute(
                    "SELECT id, parent_agent_id, workspace_id FROM agents WHERE conversation_id=? AND id != ?",
                    (conversation_id, agent_id),
                ).fetchone()
                account_owner = self.conn.execute(
                    "SELECT agent_id, account_id FROM agent_conversations WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()
                if owner or account_owner:
                    raise Busy("conversation is already owned by another agent, parent, or workspace")
            self.conn.execute(
                "UPDATE agents SET conversation_id=?, checkpoint=NULL, last_seen_at=? WHERE id=?",
                (conversation_id, now(), agent_id),
            )
            result = dict(self.conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone())
            self.conn.execute("COMMIT")
            return result
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def resume_fallback(self, agent_id: str, parent_agent_id: str, workspace: str, checkpoint: str = "", account_id: str | None = None) -> dict[str, Any]:
        if len(checkpoint) > 12_000:
            raise ValueError("checkpoint is too large")
        wsid = workspace_key(workspace)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT conversation_generation, status FROM agents WHERE id=? AND parent_agent_id=? AND workspace_id=?", (agent_id, parent_agent_id, wsid)).fetchone()
            if not row:
                raise NotFound("agent is not owned by this parent in this workspace")
            if row["status"] == "dead":
                raise Busy("agent is dead")
            if account_id:
                mapped = self.conn.execute(
                    "SELECT conversation_generation FROM agent_conversations WHERE agent_id=? AND account_id=?",
                    (agent_id, account_id),
                ).fetchone()
                generation = int(mapped[0]) + 1 if mapped else 2
                self.conn.execute(
                    "INSERT INTO agent_conversations(agent_id,account_id,conversation_id,conversation_generation,checkpoint,last_used_at) "
                    "VALUES(?,?,NULL,?,?,?) ON CONFLICT(agent_id,account_id) DO UPDATE SET "
                    "conversation_id=NULL, conversation_generation=excluded.conversation_generation, "
                    "checkpoint=excluded.checkpoint, last_used_at=excluded.last_used_at",
                    (agent_id, account_id, generation, checkpoint, now()),
                )
                self.conn.execute("COMMIT")
                return {"agent_id": agent_id, "account_id": account_id, "conversation_id": None, "conversation_generation": generation, "checkpoint": checkpoint}
            generation = int(row["conversation_generation"]) + 1
            self.conn.execute("UPDATE agents SET conversation_id=NULL, conversation_generation=?, checkpoint=?, last_seen_at=? WHERE id=?", (generation, checkpoint, now(), agent_id))
            self.conn.execute("COMMIT")
            return {"agent_id": agent_id, "conversation_id": None, "conversation_generation": generation, "checkpoint": checkpoint}
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def send(self, workspace: str, from_agent: str, to_agent: str, content: str, *, task_id: str | None = None, message_type: str = "message") -> dict[str, Any]:
        if message_type not in {"message", "request", "response", "handoff"}:
            raise ValueError("invalid message type")
        if not content.strip() or len(content) > MAX_MESSAGE_CHARS:
            raise ValueError(f"message must be 1-{MAX_MESSAGE_CHARS} characters")
        wid = workspace_key(workspace)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if not self.conn.execute("SELECT 1 FROM agents WHERE id=? AND workspace_id=?", (from_agent, wid)).fetchone():
                raise NotFound("sender is not known in this workspace")
            if not self.conn.execute("SELECT 1 FROM agents WHERE id=? AND workspace_id=?", (to_agent, wid)).fetchone():
                raise NotFound("recipient is not known in this workspace")
            if task_id and not self.conn.execute("SELECT 1 FROM tasks WHERE id=? AND workspace_id=?", (task_id, wid)).fetchone():
                raise NotFound("message task is missing or belongs to another workspace")
            message_id = "msg-" + uuid.uuid4().hex
            self.conn.execute(
                "INSERT INTO messages(id,workspace_id,task_id,from_agent,to_agent,type,content,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (message_id, wid, task_id, from_agent, to_agent, message_type, content, now()),
            )
            result = dict(self.conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone())
            self.conn.execute("COMMIT")
            return result
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def inbox(self, workspace: str, to_agent: str, *, from_agent: str | None = None, task_id: str | None = None, unread_only: bool = True, limit: int = 50, mark_read: bool = True) -> list[dict[str, Any]]:
        if limit <= 0 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        wid = workspace_key(workspace)
        clauses = ["workspace_id=?", "to_agent=?"]
        params: list[Any] = [wid, to_agent]
        if from_agent:
            clauses.append("from_agent=?")
            params.append(from_agent)
        if unread_only:
            clauses.append("read_at IS NULL")
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        if mark_read:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self.conn.execute(f"SELECT * FROM messages WHERE {' AND '.join(clauses)} ORDER BY created_at, id LIMIT ?", (*params, limit)).fetchall()
            result = [dict(row) for row in rows]
            if mark_read and result:
                at = now()
                self.conn.executemany("UPDATE messages SET read_at=? WHERE id=? AND read_at IS NULL", ((at, row["id"]) for row in result))
            if mark_read:
                self.conn.execute("COMMIT")
            return result
        except Exception:
            if mark_read:
                self.conn.execute("ROLLBACK")
            raise

    def wait(self, workspace: str, to_agent: str, *, from_agent: str | None = None, task_id: str | None = None, timeout: float = 120.0) -> list[dict[str, Any]]:
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        deadline = time.monotonic() + timeout
        delay = 0.1
        while True:
            wid = workspace_key(workspace)
            clauses = ["workspace_id=?", "to_agent=?", "read_at IS NULL"]
            params: list[Any] = [wid, to_agent]
            if from_agent:
                clauses.append("from_agent=?")
                params.append(from_agent)
            if task_id:
                clauses.append("task_id=?")
                params.append(task_id)
            rows = self.conn.execute(f"SELECT * FROM messages WHERE {' AND '.join(clauses)} ORDER BY created_at, id LIMIT 50", params).fetchall()
            if rows:
                return self.inbox(workspace, to_agent, from_agent=from_agent, task_id=task_id, limit=50)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            time.sleep(min(delay, remaining))
            delay = min(delay * 1.7, 1.0)

    def list_agents(self, workspace: str, parent_agent_id: str | None = None) -> list[dict[str, Any]]:
        wid = workspace_key(workspace)
        if parent_agent_id:
            rows = self.conn.execute("SELECT * FROM agents WHERE workspace_id=? AND parent_agent_id=? ORDER BY last_used_at DESC", (wid, parent_agent_id)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM agents WHERE workspace_id=? ORDER BY last_used_at DESC", (wid,)).fetchall()
        return [dict(row) for row in rows]


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, default=str))


def cli(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Polyphony persistent agent registry/message bus")
    parser.add_argument("--db", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    p = sub.add_parser("claim")
    p.add_argument("--parent", required=True); p.add_argument("--workspace", required=True); p.add_argument("--summary", required=True)
    p.add_argument("--agent"); p.add_argument("--parent-task"); p.add_argument("--account"); p.add_argument("--fresh-agent", action="store_true"); p.add_argument("--lease", type=int, default=DEFAULT_LEASE_SECONDS)
    p = sub.add_parser("heartbeat"); p.add_argument("task"); p.add_argument("token"); p.add_argument("--lease", type=int, default=DEFAULT_LEASE_SECONDS)
    p = sub.add_parser("finish"); p.add_argument("task"); p.add_argument("token"); p.add_argument("--status", default="completed")
    p = sub.add_parser("send"); p.add_argument("--workspace", required=True); p.add_argument("--from", dest="sender", required=True); p.add_argument("--to", required=True); p.add_argument("--message", required=True); p.add_argument("--task"); p.add_argument("--type", default="message")
    p = sub.add_parser("inbox"); p.add_argument("--workspace", required=True); p.add_argument("--to", required=True); p.add_argument("--from", dest="sender"); p.add_argument("--task"); p.add_argument("--all", action="store_true"); p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("wait"); p.add_argument("--workspace", required=True); p.add_argument("--to", required=True); p.add_argument("--from", dest="sender"); p.add_argument("--task"); p.add_argument("--timeout", type=float, default=120)
    p = sub.add_parser("agents"); p.add_argument("--workspace", required=True); p.add_argument("--parent")
    p = sub.add_parser("resume-fallback"); p.add_argument("--agent", required=True); p.add_argument("--parent", required=True); p.add_argument("--workspace", required=True); p.add_argument("--checkpoint", default=""); p.add_argument("--account")
    p = sub.add_parser("handoff"); p.add_argument("task"); p.add_argument("--from", dest="sender", required=True); p.add_argument("--to", dest="recipient", required=True); p.add_argument("--token", required=True); p.add_argument("--lease", type=int, default=DEFAULT_LEASE_SECONDS); p.add_argument("--max-hops", type=int, default=DEFAULT_MAX_HOPS)
    args = parser.parse_args(list(argv) if argv is not None else None)
    runtime = Runtime(args.db)
    try:
        if args.command == "init": result = {"db": str(runtime.path), "schema_version": SCHEMA_VERSION}
        elif args.command == "claim": result = runtime.claim_task(args.parent, args.workspace, args.summary, agent_id=args.agent, parent_task_id=args.parent_task, fresh_agent=args.fresh_agent, lease_seconds=args.lease, account_id=args.account)
        elif args.command == "heartbeat": result = {"ok": runtime.heartbeat(args.task, args.token, args.lease)}
        elif args.command == "finish": result = {"ok": runtime.finish_task(args.task, args.token, args.status)}
        elif args.command == "send": result = runtime.send(args.workspace, args.sender, args.to, args.message, task_id=args.task, message_type=args.type)
        elif args.command == "inbox": result = runtime.inbox(args.workspace, args.to, from_agent=args.sender, task_id=args.task, unread_only=not args.all, limit=args.limit)
        elif args.command == "wait": result = runtime.wait(args.workspace, args.to, from_agent=args.sender, task_id=args.task, timeout=args.timeout)
        elif args.command == "agents": result = runtime.list_agents(args.workspace, args.parent)
        elif args.command == "resume-fallback": result = runtime.resume_fallback(args.agent, args.parent, args.workspace, args.checkpoint, account_id=args.account)
        elif args.command == "handoff": result = runtime.handoff_task(args.task, args.sender, args.recipient, args.token, args.lease, args.max_hops)
        else: raise AssertionError(args.command)
        _json(result); return 0
    except (RuntimeErrorBase, ValueError, sqlite3.Error) as exc:
        print(f"polyphony-runtime: {exc}", file=sys.stderr); return 2
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(cli())
