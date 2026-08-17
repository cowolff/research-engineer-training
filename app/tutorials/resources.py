"""Runtime reader for the read-only resource index built by
tools/build_resource_index.py (docs §7.2). This module never writes to
resources.sqlite — it's a separate, build-time-baked file from the app's own
SQLite database. See docs §7.3 for the four-source selection order this
implements.
"""

import os
import re
import sqlite3

_ROW_FIELDS = ("id", "title", "url", "kind", "minutes", "band", "summary", "good_for", "rank", "checked", "link_status")


def _index_path(app):
    return app.app_config.resource_index_path


def resource_index_available(app):
    return os.path.exists(_index_path(app))


def _connect(app):
    path = _index_path(app)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row):
    return {k: row[k] for k in _ROW_FIELDS}


def get_resource(app, resource_id):
    if not resource_index_available(app):
        return None
    conn = _connect(app)
    try:
        row = conn.execute("SELECT * FROM resources WHERE id = ?", (resource_id,)).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def list_resources(app):
    if not resource_index_available(app):
        return []
    conn = _connect(app)
    try:
        rows = conn.execute("SELECT * FROM resources ORDER BY id").fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def concepts_for_resource(app, resource_id):
    conn = _connect(app)
    try:
        rows = conn.execute(
            "SELECT concept_id, relation FROM resource_concepts WHERE resource_id = ?", (resource_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


_FTS_UNSAFE = re.compile(r'["\'\-*^]')


def _fts_query_text(*texts):
    """Builds a permissive OR-of-terms FTS5 MATCH query from free text,
    stripping characters FTS5's query syntax treats specially so scenario/
    rubric text can't produce a malformed or unintentionally strict query."""
    words = set()
    for text in texts:
        for word in _FTS_UNSAFE.sub(" ", text or "").split():
            word = word.strip().lower()
            if len(word) > 2:
                words.add(word)
    if not words:
        return None
    return " OR ".join(f'"{w}"' for w in words)


def select_resources(app, concept, weak_concept_ids=None, scenario=None, student_band=2, max_n=None):
    """Implements docs §7.3's four-source priority: direct, prerequisite,
    adjacent, scenario-specific. Returns a list of resource dicts, capped at
    max_n (default: app_config.resource_shortlist_max)."""
    if not resource_index_available(app):
        return []

    max_n = max_n or app.app_config.resource_shortlist_max
    weak_concept_ids = weak_concept_ids or []
    conn = _connect(app)
    picked = {}

    try:
        # 1. Direct — resources that teach the target concept.
        rows = conn.execute(
            """SELECT r.* FROM resources r
               JOIN resource_concepts rc ON rc.resource_id = r.id
               WHERE rc.concept_id = ? AND rc.relation = 'teaches' AND r.band <= ?
               ORDER BY r.rank ASC""",
            (concept.id, student_band + 1),
        ).fetchall()
        for row in rows:
            picked.setdefault(row["id"], _row_to_dict(row))
            if len(picked) >= max_n:
                return list(picked.values())

        # 2. Prerequisite — resources that assume a concept the student is weak on.
        for weak_id in weak_concept_ids:
            rows = conn.execute(
                """SELECT r.* FROM resources r
                   JOIN resource_concepts rc ON rc.resource_id = r.id
                   WHERE rc.concept_id = ? AND rc.relation = 'assumes'
                   ORDER BY r.rank ASC""",
                (weak_id,),
            ).fetchall()
            for row in rows:
                picked.setdefault(row["id"], _row_to_dict(row))
                if len(picked) >= max_n:
                    return list(picked.values())

        # 3. Adjacent — resources for curriculum-related concepts, capped at 2.
        added_adjacent = 0
        for related_id in concept.related:
            if added_adjacent >= 2:
                break
            rows = conn.execute(
                """SELECT r.* FROM resources r
                   JOIN resource_concepts rc ON rc.resource_id = r.id
                   WHERE rc.concept_id = ? AND rc.relation = 'teaches'
                   ORDER BY r.rank ASC LIMIT 1""",
                (related_id,),
            ).fetchall()
            for row in rows:
                if row["id"] not in picked:
                    picked[row["id"]] = _row_to_dict(row)
                    added_adjacent += 1
                    if len(picked) >= max_n:
                        return list(picked.values())

        # 4. Scenario-specific — an FTS5 query over the scenario's own text, capped at 2.
        # Two queries rather than a join: resources_fts's hidden `rank` column
        # (match quality) would otherwise collide by name with the curator's
        # own `resources.rank` column in the combined result set.
        if scenario is not None:
            expected_text = " ".join(item.get("expected", "") for item in scenario.rubric)
            query = _fts_query_text(scenario.title, expected_text)
            if query:
                fts_rows = conn.execute(
                    "SELECT rowid FROM resources_fts WHERE resources_fts MATCH ? ORDER BY rank LIMIT 2",
                    (query,),
                ).fetchall()
                for fts_row in fts_rows:
                    row = conn.execute("SELECT * FROM resources WHERE rowid = ?", (fts_row["rowid"],)).fetchone()
                    if row:
                        picked.setdefault(row["id"], _row_to_dict(row))
                    if len(picked) >= max_n:
                        break
    finally:
        conn.close()

    return list(picked.values())[:max_n]
