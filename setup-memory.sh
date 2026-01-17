#!/bin/bash
# Setup script for Kronk's memory system

set -e

MEMORY_DIR="./kronk_memory"

echo "Setting up Kronk memory system..."

# Create memory directory
mkdir -p "$MEMORY_DIR"

# Create empty JSON files
echo "{}" > "$MEMORY_DIR/user_summaries.json"
echo "[]" > "$MEMORY_DIR/conversations.json"

echo "Created memory directory: $MEMORY_DIR"
echo "  - user_summaries.json"
echo "  - conversations.json"

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
