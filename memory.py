"""
Lightweight memory system for Kronk using Ollama embeddings for semantic search
and LLM-generated user summaries.

Uses Ollama's embedding API + simple JSON storage - no heavy dependencies.
"""

import json
import hashlib
from pathlib import Path
from datetime import datetime

import ollama

# === Storage Setup ===

MEMORY_DIR = Path("./kronk_memory")
MEMORY_DIR.mkdir(exist_ok=True)

USER_SUMMARIES_FILE = MEMORY_DIR / "user_summaries.json"
CONVERSATIONS_FILE = MEMORY_DIR / "conversations.json"

EMBEDDING_MODEL = "nomic-embed-text"  # Small, fast embedding model


# === Embedding Utilities ===

def get_embedding(text: str) -> list[float]:
    """Get embedding vector from Ollama."""
    response = ollama.embed(model=EMBEDDING_MODEL, input=text)
    return response["embeddings"][0]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot_product = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot_product / (norm_a * norm_b)


# === User Summaries (Option 3) ===

def load_user_summaries() -> dict:
    """Load all user summaries from disk."""
    if USER_SUMMARIES_FILE.exists():
        return json.loads(USER_SUMMARIES_FILE.read_text())
    return {}


def save_user_summaries(summaries: dict):
    """Save user summaries to disk."""
    USER_SUMMARIES_FILE.write_text(json.dumps(summaries, indent=2))


def get_user_summary(user_id: str) -> str | None:
    """Get the stored summary for a specific user."""
    summaries = load_user_summaries()
    user_data = summaries.get(str(user_id))
    if user_data:
        return user_data.get("summary")
    return None


def update_user_summary(user_id: str, summary: str):
    """Update the summary for a specific user."""
    summaries = load_user_summaries()
    summaries[str(user_id)] = {
        "summary": summary,
        "updated_at": datetime.now().isoformat()
    }
    save_user_summaries(summaries)


async def generate_user_summary(
    user_id: str,
    user_name: str,
    recent_messages: list[dict],
    model: str
) -> str:
    """
    Ask the LLM to summarise what it knows about a user based on recent messages.
    Stores the summary and returns it.
    """
    # Filter to just this user's messages
    user_messages = [
        m["content"] for m in recent_messages
        if m["role"] == "user" and m["content"].startswith(f"{user_name}:")
    ]

    if not user_messages:
        return ""

    # Get existing summary to build upon
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


# === Conversation Memory (Option 2) ===

def load_conversations() -> list[dict]:
    """Load all stored conversations."""
    if CONVERSATIONS_FILE.exists():
        return json.loads(CONVERSATIONS_FILE.read_text())
    return []


def save_conversations(conversations: list[dict]):
    """Save conversations to disk."""
    CONVERSATIONS_FILE.write_text(json.dumps(conversations, indent=2))


def generate_conv_id(channel_id: str, timestamp: str) -> str:
    """Generate a unique ID for a conversation snippet."""
    raw = f"{channel_id}:{timestamp}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def store_conversation(
    channel_id: str,
    messages: list[dict],
    summary: str = None
):
    """
    Store a conversation snippet for later semantic retrieval.
    If no summary provided, uses the raw messages as the document.
    """
    timestamp = datetime.now().isoformat()
    conv_id = generate_conv_id(str(channel_id), timestamp)

    # Create a text representation of the conversation
    if summary:
        document = summary
    else:
        document = "\n".join([
            f"{m['role']}: {m['content'][:200]}"
            for m in messages[-5:]  # Last 5 messages
        ])

    # Generate embedding for semantic search
    embedding = get_embedding(document)

    conversations = load_conversations()
    conversations.append({
        "id": conv_id,
        "document": document,
        "embedding": embedding,
        "channel_id": str(channel_id),
        "timestamp": timestamp,
        "message_count": len(messages)
    })

    # Keep only last 500 conversations to prevent unbounded growth
    if len(conversations) > 500:
        conversations = conversations[-500:]

    save_conversations(conversations)


def recall_relevant_conversations(
    query: str,
    n_results: int = 3,
    channel_id: str = None
) -> list[str]:
    """
    Find past conversations semantically relevant to the current query.
    Optionally filter by channel.
    """
    conversations = load_conversations()
    if not conversations:
        return []

    # Filter by channel if specified
    if channel_id:
        conversations = [c for c in conversations if c["channel_id"] == str(channel_id)]

    if not conversations:
        return []

    # Get query embedding and compute similarities
    query_embedding = get_embedding(query)

    scored = []
    for conv in conversations:
        similarity = cosine_similarity(query_embedding, conv["embedding"])
        scored.append((similarity, conv["document"]))

    # Sort by similarity (highest first) and return top n
    scored.sort(reverse=True, key=lambda x: x[0])

    # Only return if similarity is above threshold
    threshold = 0.3
    results = [doc for score, doc in scored[:n_results] if score > threshold]

    return results


async def generate_conversation_summary(
    messages: list[dict],
    model: str
) -> str:
    """Generate a brief summary of a conversation for storage."""
    conversation_text = "\n".join([
        f"{m['role']}: {m['content'][:300]}"
        for m in messages[-10:]
    ])

    prompt = f"""Summarize this conversation in 2-3 sentences. Focus on the main topic and any important information exchanged.

{conversation_text}"""

    client = ollama.AsyncClient()
    response = await client.chat(
        model=model,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.message.content


# === Helper for main.py ===

def build_memory_context(
    user_id: str,
    current_message: str,
    channel_id: str = None
) -> str | None:
    """
    Build a memory context string to inject into the system prompt.
    Returns None if no relevant memories found.
    """
    context_parts = []

    # Add user summary if available
    user_summary = get_user_summary(user_id)
    if user_summary:
        context_parts.append(f"About this user: {user_summary}")

    # Add relevant past conversations
    relevant = recall_relevant_conversations(
        current_message,
        n_results=2,
        channel_id=channel_id
    )
    if relevant:
        context_parts.append("Relevant past conversations:\n" + "\n---\n".join(relevant))

    if context_parts:
        return "\n\n".join(context_parts)
    return None
