"""Loads curriculum/topics.yaml and upserts it into the topics/concepts tables.

Idempotent by design: re-running against an unchanged file changes nothing,
and bumping `version` in the YAML is how a curriculum edit is rolled out (a
redeploy, per docs/IMPLEMENTATION_PLAN.md's "curriculum ownership" decision).
"""

import yaml

from app.db import db
from app.models import Topic, Concept


class CurriculumError(ValueError):
    pass


def load_curriculum_yaml(path):
    with open(path) as f:
        data = yaml.safe_load(f)

    version = data.get("version")
    topics = data.get("topics", [])
    if not version or not topics:
        raise CurriculumError(f"{path}: missing 'version' or empty 'topics'")

    all_concept_ids = set()
    for topic in topics:
        for concept in topic.get("concepts", []):
            all_concept_ids.add(concept["id"])

    for topic in topics:
        for concept in topic.get("concepts", []):
            for related_id in concept.get("related", []):
                if related_id not in all_concept_ids:
                    raise CurriculumError(
                        f"{path}: concept '{concept['id']}' references unknown related concept '{related_id}'"
                    )

    return version, topics


def seed_curriculum(path="curriculum/topics.yaml"):
    version, topics = load_curriculum_yaml(path)

    topic_count = 0
    concept_count = 0

    for topic_data in topics:
        topic = db.session.get(Topic, topic_data["id"])
        if topic is None:
            topic = Topic(id=topic_data["id"])
            db.session.add(topic)
        topic.title = topic_data["title"]
        topic.band = topic_data.get("band", 1)
        topic.curriculum_version = version
        topic_count += 1

        for concept_data in topic_data.get("concepts", []):
            concept = db.session.get(Concept, concept_data["id"])
            if concept is None:
                concept = Concept(id=concept_data["id"])
                db.session.add(concept)
            concept.topic_id = topic.id
            concept.name = concept_data["name"]
            concept.essential = bool(concept_data.get("essential", False))
            concept.probe = concept_data.get("probe", "")
            concept.aliases_json = concept_data.get("aliases", [])
            concept.related_json = concept_data.get("related", [])
            concept.curriculum_version = version
            concept_count += 1

    db.session.commit()
    return {"version": version, "topics": topic_count, "concepts": concept_count}
