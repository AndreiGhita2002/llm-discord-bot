#!/bin/bash
# Setup script for bot memory system

set -e

MEMORY_DIR="./bot_memory"

echo "Setting up bot memory system..."

# Create memory directory (the SQLite store `memory.db` is created automatically on first run)
mkdir -p "$MEMORY_DIR"

echo "Created memory directory: $MEMORY_DIR"
echo "  - memory.db will be created here on first run (SQLite + sqlite-vec)"

# Check if embedding model is available
if command -v ollama &> /dev/null; then
    echo ""
    echo "Checking for embedding model..."
    if ollama list | grep -q "nomic-embed-text"; then
        echo "nomic-embed-text model found."
    else
        echo "nomic-embed-text not found. Pulling..."
        ollama pull nomic-embed-text
    fi
else
    echo ""
    echo "Warning: ollama not found in PATH"
    echo "Run 'ollama pull nomic-embed-text' manually before starting the bot."
fi

echo ""
echo "Memory setup complete!"
