#!/usr/bin/env python3
"""Compiles resources/resources.yaml into a read-only SQLite index, validated
against curriculum/topics.yaml. Hermetic — no network calls, so the Docker
build never depends on an external site being reachable. See
docs/IMPLEMENTATION_PLAN.md §7.2.

Usage: python tools/build_resource_index.py --in resources/resources.yaml \
           --curriculum curriculum/topics.yaml --out app/data/resources.sqlite
"""

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

import yaml

VALID_KINDS = {"docs", "tutorial", "video", "talk", "paper", "book", "tool", "cheatsheet"}


def load_curriculum_concepts(path):
    data = yaml.safe_load(Path(path).read_text())
    essential_by_id = {}
    for topic in data.get("topics", []):
        for concept in topic.get("concepts", []):
            essential_by_id[concept["id"]] = bool(concept.get("essential", False))
    return essential_by_id


def validate(resources, concept_essential):
    errors = []
    seen_ids = set()
    concept_has_resource = set()

    for r in resources:
        rid = r.get("id")
        if not rid:
            errors.append("a resource is missing 'id'")
            continue
        if rid in seen_ids:
            errors.append(f"{rid}: duplicate resource id")
        seen_ids.add(rid)

        parsed = urlparse(r.get("url", ""))
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            errors.append(f"{rid}: malformed url {r.get('url')!r}")

        if not (r.get("summary") or "").strip():
            errors.append(f"{rid}: missing summary")

        if r.get("kind") not in VALID_KINDS:
            errors.append(f"{rid}: invalid kind {r.get('kind')!r} (expected one of {sorted(VALID_KINDS)})")

        for cid in list(r.get("concepts", [])) + list(r.get("assumes", [])):
            if cid not in concept_essential:
                errors.append(f"{rid}: references unknown concept_id '{cid}'")
        for cid in r.get("concepts", []):
            concept_has_resource.add(cid)

    for cid, essential in concept_essential.items():
        if essential and cid not in concept_has_resource:
            errors.append(f"essential concept '{cid}' has zero resources")

    return errors


def build(resources, out_path):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    conn = sqlite3.connect(str(out_path))
    conn.executescript(
        """
        CREATE TABLE resources (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            kind TEXT NOT NULL,
            minutes INTEGER NOT NULL DEFAULT 0,
            band INTEGER NOT NULL DEFAULT 1,
            summary TEXT NOT NULL,
            good_for TEXT NOT NULL DEFAULT '',
            rank INTEGER NOT NULL DEFAULT 1,
            checked TEXT,
            link_status TEXT NOT NULL DEFAULT 'unchecked'
        );
        CREATE TABLE resource_concepts (
            resource_id TEXT NOT NULL,
            concept_id TEXT NOT NULL,
            relation TEXT NOT NULL
        );
        CREATE INDEX idx_resource_concepts_concept ON resource_concepts(concept_id);
        CREATE INDEX idx_resource_concepts_resource ON resource_concepts(resource_id);
        CREATE VIRTUAL TABLE resources_fts USING fts5(title, summary, good_for, content='resources');
        CREATE TABLE index_meta (
            source_hash TEXT NOT NULL,
            resources_count INTEGER NOT NULL,
            concepts_count INTEGER NOT NULL
        );
        """
    )

    # Sorted insertion order (not YAML file order) is part of what makes the
    # build deterministic: two authors reordering resources.yaml differently
    # still produce the same index contents.
    for r in sorted(resources, key=lambda x: x["id"]):
        conn.execute(
            """INSERT INTO resources
               (id, title, url, kind, minutes, band, summary, good_for, rank, checked, link_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                r["id"], r["title"], r["url"], r["kind"], r.get("minutes", 0), r.get("band", 1),
                r["summary"].strip(), r.get("good_for", ""), r.get("rank", 1), r.get("checked"), "unchecked",
            ),
        )
        for cid in r.get("concepts", []):
            conn.execute("INSERT INTO resource_concepts VALUES (?,?,?)", (r["id"], cid, "teaches"))
        for cid in r.get("assumes", []):
            conn.execute("INSERT INTO resource_concepts VALUES (?,?,?)", (r["id"], cid, "assumes"))

    conn.execute("INSERT INTO resources_fts(resources_fts) VALUES ('rebuild')")

    concepts_count = conn.execute(
        "SELECT COUNT(DISTINCT concept_id) FROM resource_concepts WHERE relation = 'teaches'"
    ).fetchone()[0]
    source_hash = hashlib.sha256(json.dumps(resources, sort_keys=True, default=str).encode()).hexdigest()[:16]
    conn.execute("INSERT INTO index_meta VALUES (?,?,?)", (source_hash, len(resources), concepts_count))

    conn.commit()
    conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="in_path", default="resources/resources.yaml")
    parser.add_argument("--curriculum", default="curriculum/topics.yaml")
    parser.add_argument("--out", default="app/data/resources.sqlite")
    args = parser.parse_args()

    data = yaml.safe_load(Path(args.in_path).read_text())
    resources = data.get("resources", [])
    concept_essential = load_curriculum_concepts(args.curriculum)

    errors = validate(resources, concept_essential)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        print(f"\n{len(errors)} error(s) — refusing to build.", file=sys.stderr)
        sys.exit(1)

    build(resources, args.out)
    print(f"Built {args.out}: {len(resources)} resources.")


if __name__ == "__main__":
    main()
