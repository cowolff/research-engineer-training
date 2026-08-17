import os
import sqlite3
import sys

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import build_resource_index as indexer  # noqa: E402


CURRICULUM = {
    "version": 1,
    "topics": [
        {
            "id": "t1",
            "title": "Topic One",
            "band": 1,
            "concepts": [
                {"id": "c-essential", "name": "Essential concept", "essential": True, "related": []},
                {"id": "c-optional", "name": "Optional concept", "essential": False, "related": []},
            ],
        }
    ],
}


def _valid_resources():
    return {
        "version": 1,
        "resources": [
            {
                "id": "r1",
                "title": "Resource one",
                "url": "https://example.com/one",
                "kind": "docs",
                "minutes": 10,
                "band": 1,
                "concepts": ["c-essential"],
                "summary": "A summary.",
                "good_for": "Learning",
                "rank": 1,
            }
        ],
    }


def _write(tmp_path, curriculum, resources):
    curriculum_path = tmp_path / "topics.yaml"
    resources_path = tmp_path / "resources.yaml"
    curriculum_path.write_text(yaml.safe_dump(curriculum))
    resources_path.write_text(yaml.safe_dump(resources))
    return curriculum_path, resources_path


def _dump_logical_contents(db_path):
    conn = sqlite3.connect(str(db_path))
    resources = conn.execute("SELECT * FROM resources ORDER BY id").fetchall()
    concepts = conn.execute("SELECT * FROM resource_concepts ORDER BY resource_id, concept_id, relation").fetchall()
    conn.close()
    return resources, concepts


def test_build_succeeds_on_valid_input(tmp_path):
    curriculum_path, resources_path = _write(tmp_path, CURRICULUM, _valid_resources())
    out_path = tmp_path / "out.sqlite"

    concept_essential = indexer.load_curriculum_concepts(curriculum_path)
    resources = yaml.safe_load(resources_path.read_text())["resources"]
    errors = indexer.validate(resources, concept_essential)
    assert errors == []

    indexer.build(resources, out_path)
    assert out_path.exists()


def test_build_is_deterministic(tmp_path):
    _, resources_path = _write(tmp_path, CURRICULUM, _valid_resources())
    resources = yaml.safe_load(resources_path.read_text())["resources"]

    out1 = tmp_path / "out1.sqlite"
    out2 = tmp_path / "out2.sqlite"
    indexer.build(resources, out1)
    indexer.build(resources, out2)

    assert _dump_logical_contents(out1) == _dump_logical_contents(out2)


def test_unknown_concept_id_fails_validation(tmp_path):
    curriculum_path, _ = _write(tmp_path, CURRICULUM, _valid_resources())
    concept_essential = indexer.load_curriculum_concepts(curriculum_path)

    resources = _valid_resources()["resources"]
    resources[0]["concepts"] = ["this-concept-does-not-exist"]

    errors = indexer.validate(resources, concept_essential)
    assert any("unknown concept_id" in e for e in errors)


def test_essential_concept_with_no_resources_fails_validation(tmp_path):
    curriculum_path, _ = _write(tmp_path, CURRICULUM, _valid_resources())
    concept_essential = indexer.load_curriculum_concepts(curriculum_path)

    resources = _valid_resources()["resources"]
    resources[0]["concepts"] = ["c-optional"]  # leaves c-essential uncovered

    errors = indexer.validate(resources, concept_essential)
    assert any("c-essential" in e and "zero resources" in e for e in errors)


def test_malformed_url_and_missing_summary_fail_validation(tmp_path):
    curriculum_path, _ = _write(tmp_path, CURRICULUM, _valid_resources())
    concept_essential = indexer.load_curriculum_concepts(curriculum_path)

    resources = _valid_resources()["resources"]
    resources[0]["url"] = "not-a-url"
    resources[0]["summary"] = ""

    errors = indexer.validate(resources, concept_essential)
    assert any("malformed url" in e for e in errors)
    assert any("missing summary" in e for e in errors)


def test_duplicate_resource_id_fails_validation(tmp_path):
    curriculum_path, _ = _write(tmp_path, CURRICULUM, _valid_resources())
    concept_essential = indexer.load_curriculum_concepts(curriculum_path)

    data = _valid_resources()["resources"]
    resources = data + [dict(data[0])]  # same id twice

    errors = indexer.validate(resources, concept_essential)
    assert any("duplicate resource id" in e for e in errors)


def test_real_curriculum_and_resources_pass_validation():
    """The actual seed data ships correctly, not just the toy fixtures above."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    curriculum_path = os.path.join(repo_root, "curriculum", "topics.yaml")
    resources_path = os.path.join(repo_root, "resources", "resources.yaml")

    concept_essential = indexer.load_curriculum_concepts(curriculum_path)
    resources = yaml.safe_load(open(resources_path))["resources"]
    errors = indexer.validate(resources, concept_essential)
    assert errors == []
