#!/usr/bin/env python3
"""
Convert MCP Memory Server JSONL (knowledge graph format)
→ Odysseus memory.json (flat memory entry format)

Usage:
    python3 convert-mcp-memory-to-odysseus.py /path/to/memory.jsonl

Output: appends to /mnt/smalldata/share/projects/odysseus/data/memory.json
"""

import json
import os
import sys
import uuid
import time

ODYSSEUS_MEMORY_FILE = "/mnt/smalldata/share/projects/odysseus/data/memory.json"


def load_mcp_jsonl(path: str) -> dict:
    """Load the MCP knowledge graph from a JSONL file."""
    entities = []
    relations = []

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            if item.get("type") == "entity":
                entities.append(item)
            elif item.get("type") == "relation":
                relations.append(item)

    return {"entities": entities, "relations": relations}


def convert_to_odysseus_entries(knowledge_graph: dict) -> list:
    """Convert knowledge graph entities+observations → Odysseus memory entries."""
    entries = []
    seen_texts = set()

    # --- Entities and their observations become memory entries ---
    for entity in knowledge_graph["entities"]:
        name = entity.get("name", "")
        entity_type = entity.get("entityType", "")
        observations = entity.get("observations", [])

        # Each entity itself becomes a memory
        entity_text = f"{name} is a {entity_type}" if entity_type else name
        if entity_text not in seen_texts:
            seen_texts.add(entity_text)
            entries.append({
                "id": str(uuid.uuid4()),
                "text": entity_text,
                "timestamp": int(time.time()),
                "source": "mcp_migration",
                "category": _map_category(entity_type),
                "uses": 0,
            })

        # Each observation becomes a separate memory
        for obs in observations:
            if obs not in seen_texts:
                seen_texts.add(obs)
                entries.append({
                    "id": str(uuid.uuid4()),
                    "text": f"{name}: {obs}",
                    "timestamp": int(time.time()),
                    "source": "mcp_migration",
                    "category": _map_category(entity_type),
                    "uses": 0,
                })

    # --- Relations become memory entries too ---
    for rel in knowledge_graph["relations"]:
        rel_text = f"{rel['from']} {rel['relationType']} {rel['to']}"
        if rel_text not in seen_texts:
            seen_texts.add(rel_text)
            entries.append({
                "id": str(uuid.uuid4()),
                "text": rel_text,
                "timestamp": int(time.time()),
                "source": "mcp_migration",
                "category": "fact",
                "uses": 0,
            })

    return entries


def _map_category(entity_type: str) -> str:
    """Map MCP entityType → Odysseus category."""
    mapping = {
        "person": "contact",
        "organization": "contact",
        "company": "contact",
        "event": "event",
        "goal": "goal",
        "task": "task",
        "project": "project",
        "preference": "preference",
    }
    return mapping.get(entity_type.lower(), "fact")


def merge_into_odysseus(new_entries: list):
    """Read existing memory.json, append new entries, deduplicate by text, write back."""
    existing = []
    if os.path.exists(ODYSSEUS_MEMORY_FILE):
        try:
            with open(ODYSSEUS_MEMORY_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            existing = []

    if not isinstance(existing, list):
        existing = []

    # Deduplicate by text to avoid importing existing memories again
    existing_texts = {e.get("text", "") for e in existing}

    truly_new = [e for e in new_entries if e["text"] not in existing_texts]

    merged = existing + truly_new

    # Atomic write
    tmp = ODYSSEUS_MEMORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ODYSSEUS_MEMORY_FILE)

    print(f"  Existing entries: {len(existing)}")
    print(f"  New entries imported: {len(truly_new)}")
    print(f"  Skipped (duplicates): {len(new_entries) - len(truly_new)}")
    print(f"  Total: {len(merged)}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 convert-mcp-memory-to-odysseus.py <memory.jsonl>")
        sys.exit(1)

    mcp_file = sys.argv[1]
    if not os.path.isfile(mcp_file):
        print(f"Error: file not found: {mcp_file}")
        sys.exit(1)

    print(f"📖 Reading MCP knowledge graph from: {mcp_file}")
    kg = load_mcp_jsonl(mcp_file)
    print(f"   Found {len(kg['entities'])} entities, {len(kg['relations'])} relations")

    print(f"🔄 Converting to Odysseus format...")
    entries = convert_to_odysseus_entries(kg)
    print(f"   Generated {len(entries)} memory entries")

    print(f"💾 Merging into: {ODYSSEUS_MEMORY_FILE}")
    merge_into_odysseus(entries)
    print(f"✅ Done! Restart the memory server to pick up changes:")
    print(f"   sudo systemctl restart odysseus-memory-sse")


if __name__ == "__main__":
    main()
