"""
Lightweight memory system for Discord bots.

Backed by SQLite + the sqlite-vec extension for efficient on-disk vector search:
  - embeddings are stored as packed float32 blobs (not JSON text) and indexed for KNN
  - conversation/summary text is stored zlib-compressed (smaller + not plaintext on disk)

This is far more space/CPU efficient than the old load-whole-JSON-and-scan approach, and the
binary .db file isn't casually human-readable. The public API is unchanged, so main.py/tools.py
don't need to care about the storage layer.

Needs the `nomic-embed-text` embedding model in Ollama and the `sqlite-vec` package.
"""

import hashlib
import logging
import sqlite3
import struct
import threading
import zlib
from datetime import datetime
from pathlib import Path

import ollama
import sqlite_vec

log = logging.getLogger("kronk")

# === Storage Setup ===

MEMORY_DIR: Path = None
DB_PATH: Path = None

EMBEDDING_MODEL = "nomic-embed-text"  # Small, fast embedding model
EMBEDDING_DIM = 768                   # nomic-embed-text output dimension

# Retrieval threshold: keep hits whose cosine similarity > 0.3, i.e. cosine distance < 0.7.
_MAX_COSINE_DISTANCE = 0.7

_conn: sqlite3.Connection = None
_lock = threading.Lock()  # serialize DB access (recall runs in a worker thread)


def _connect(path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with the sqlite-vec extension loaded."""
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS user_summaries (
               user_id    TEXT PRIMARY KEY,
               summary    BLOB,   -- zlib-compressed utf-8
               updated_at TEXT
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS conversations (
               id            INTEGER PRIMARY KEY AUTOINCREMENT,
               conv_id       TEXT,
               channel_id    TEXT,
               timestamp     TEXT,
               message_count INTEGER,
               document      BLOB   -- zlib-compressed utf-8
           )"""
    )
    conn.execute(
        f"""CREATE VIRTUAL TABLE IF NOT EXISTS conversations_vec USING vec0(
                embedding float[{EMBEDDING_DIM}] distance_metric=cosine
            )"""
    )
    conn.commit()


def init_memory(memory_dir: str = "./bot_memory"):
    """Initialize the memory store (SQLite DB inside memory_dir).

    Falls back to the legacy 'kronk_memory/' directory if it exists and the configured one
    doesn't, for backward compatibility with the directory location (data itself starts fresh
    in memory.db; any old *.json files are left untouched).
    """
    global MEMORY_DIR, DB_PATH, _conn

    configured_path = Path(memory_dir)
    legacy_path = Path("./kronk_memory")

    if not configured_path.exists() and legacy_path.exists():
        MEMORY_DIR = legacy_path
        print(f"[memory] Using legacy directory: {legacy_path}")
    else:
        MEMORY_DIR = configured_path
        MEMORY_DIR.mkdir(exist_ok=True)

    DB_PATH = MEMORY_DIR / "memory.db"
    _conn = _connect(DB_PATH)
    _init_schema(_conn)
    print(f"[memory] SQLite store ready at {DB_PATH}")


# === Encoding Utilities ===

def _compress(text: str) -> bytes:
    """zlib-compress text (obfuscates + shrinks it on disk)."""
    return zlib.compress((text or "").encode("utf-8"))


def _decompress(blob) -> str:
    if not blob:
        return ""
    try:
        return zlib.decompress(blob).decode("utf-8")
    except (zlib.error, TypeError):
        return ""


def _serialize_vector(vec: list[float]) -> bytes:
    """Pack a float list into little-endian float32 bytes for sqlite-vec."""
    return struct.pack("<%sf" % len(vec), *vec)


def get_embedding(text: str) -> list[float]:
    """Get an embedding vector from Ollama."""
    response = ollama.embed(model=EMBEDDING_MODEL, input=text)
    return response["embeddings"][0]


# === User Summaries ===

def get_user_summary(user_id: str) -> str | None:
    """Get the stored summary for a specific user, or None."""
    if _conn is None:
        return None
    with _lock:
        row = _conn.execute(
            "SELECT summary FROM user_summaries WHERE user_id = ?", (str(user_id),)
        ).fetchone()
    if row is None:
        return None
    return _decompress(row[0])


def update_user_summary(user_id: str, summary: str):
    """Insert or update the summary for a specific user."""
    if _conn is None:
        return
    with _lock:
        _conn.execute(
            """INSERT INTO user_summaries (user_id, summary, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET summary = excluded.summary,
                                                  updated_at = excluded.updated_at""",
            (str(user_id), _compress(summary), datetime.now().isoformat()),
        )
        _conn.commit()


async def generate_user_summary(
    user_id: str,
    user_name: str,
    recent_messages: list[dict],
    model: str
) -> str:
    """Ask the LLM to summarise what it knows about a user, store it, and return it."""
    # Filter to just this user's messages
    # TODO: this prefix match is imperfect vs the "name(id)[time]:" input format; pre-existing.
    user_messages = [
        m["content"] for m in recent_messages
        if m["role"] == "user" and m["content"].startswith(f"{user_name}:")
    ]

    if not user_messages:
        return ""

    existing = get_user_summary(user_id)
    existing_context = f"Previous summary: {existing}\n\n" if existing else ""

    prompt = f"""{existing_context}Based on these recent messages from {user_name}, write a brief summary of what you know about them.
Include: personality traits, interests, how they communicate, any facts they've shared.
Keep it under 100 words. Be factual, not speculative.

Recent messages:
{chr(10).join(user_messages[-10:])}"""

    client = ollama.AsyncClient()
    response = await client.chat(
        model=model,
        messages=[{"role": "user", "content": prompt}]
    )

    summary = response.message.content
    update_user_summary(user_id, summary)
    return summary


# === Conversation Memory ===

def generate_conv_id(channel_id: str, timestamp: str) -> str:
    """Generate a stable-ish unique id for a conversation snippet."""
    raw = f"{channel_id}:{timestamp}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def store_conversation(
    channel_id: str,
    messages: list[dict],
    summary: str = None,
    max_conversations: int = 500
):
    """Store a conversation snippet (document + embedding) for later semantic retrieval."""
    if _conn is None:
        return

    timestamp = datetime.now().isoformat()
    conv_id = generate_conv_id(str(channel_id), timestamp)

    if summary:
        document = summary
    else:
        document = "\n".join(
            f"{m['role']}: {m['content'][:200]}" for m in messages[-5:]
        )

    embedding = get_embedding(document)
    if len(embedding) != EMBEDDING_DIM:
        log.warning(f"embedding dim {len(embedding)} != {EMBEDDING_DIM}; skipping store")
        return

    with _lock:
        cur = _conn.execute(
            """INSERT INTO conversations (conv_id, channel_id, timestamp, message_count, document)
               VALUES (?, ?, ?, ?, ?)""",
            (conv_id, str(channel_id), timestamp, len(messages), _compress(document)),
        )
        rowid = cur.lastrowid
        _conn.execute(
            "INSERT INTO conversations_vec (rowid, embedding) VALUES (?, ?)",
            (rowid, _serialize_vector(embedding)),
        )

        # Trim to the most recent `max_conversations` to bound growth.
        (count,) = _conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
        if count > max_conversations:
            overflow = count - max_conversations
            old_ids = [
                r[0] for r in _conn.execute(
                    "SELECT id FROM conversations ORDER BY id ASC LIMIT ?", (overflow,)
                ).fetchall()
            ]
            placeholders = ",".join("?" * len(old_ids))
            _conn.execute(f"DELETE FROM conversations WHERE id IN ({placeholders})", old_ids)
            _conn.execute(f"DELETE FROM conversations_vec WHERE rowid IN ({placeholders})", old_ids)

        _conn.commit()


def recall_relevant_conversations(
    query: str,
    n_results: int = 3,
    channel_id: str = None
) -> list[str]:
    """Find past conversations semantically relevant to the query via KNN vector search."""
    if _conn is None:
        return []

    query_embedding = get_embedding(query)
    if len(query_embedding) != EMBEDDING_DIM:
        return []
    query_blob = _serialize_vector(query_embedding)

    # Over-fetch, then apply the optional channel filter + distance threshold in Python.
    k = max(n_results * 5, 20)
    with _lock:
        rows = _conn.execute(
            """SELECT c.document, c.channel_id, v.distance
               FROM conversations_vec v
               JOIN conversations c ON c.id = v.rowid
               WHERE v.embedding MATCH ? AND k = ?
               ORDER BY v.distance""",
            (query_blob, k),
        ).fetchall()

    results = []
    for document_blob, row_channel, distance in rows:
        if channel_id is not None and row_channel != str(channel_id):
            continue
        if distance > _MAX_COSINE_DISTANCE:
            continue
        results.append(_decompress(document_blob))
        if len(results) >= n_results:
            break
    return results


# === Helper for main.py ===

def build_memory_context(
    user_id: str,
    current_message: str,
    channel_id: str = None,
    do_user_memory: bool = False,
    do_conversation_memory: bool = False,
) -> str | None:
    """Build a memory context string to inject into the system prompt, or None."""
    context_parts = []

    if do_user_memory:
        user_summary = get_user_summary(user_id)
        if user_summary:
            context_parts.append(f"About this user: {user_summary}")

    if do_conversation_memory:
        relevant = recall_relevant_conversations(
            current_message, n_results=2, channel_id=channel_id
        )
        if relevant:
            context_parts.append("Relevant past conversations:\n" + "\n---\n".join(relevant))

    if context_parts:
        return "\n\n".join(context_parts)
    return None
