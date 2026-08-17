from datetime import datetime

from flask import Blueprint, render_template, abort, jsonify, current_app, url_for
from flask_login import login_required, current_user

from app.db import db
from app.models import Tutorial, TutorialRead, Concept, Topic, TutorialLink, ConceptEvent, Attempt, Scenario
from app.render import render_markdown
from app.tutorials.resources import get_resource

bp = Blueprint("tutorials", __name__)


@bp.get("/tutorials")
@login_required
def library():
    reads = db.session.query(TutorialRead).filter_by(user_id=current_user.id).all()

    groups = {}
    for read in reads:
        tutorial = db.session.get(Tutorial, read.tutorial_id)
        if tutorial is None:
            continue
        concept = db.session.get(Concept, tutorial.concept_id)
        topic = db.session.get(Topic, concept.topic_id) if concept else None
        key = topic.id if topic else "other"
        groups.setdefault(key, {"title": topic.title if topic else "Other", "items": []})
        groups[key]["items"].append({"tutorial": tutorial, "read": read, "concept": concept})

    return render_template("tutorials/library.html", groups=groups)


def _related_concept_ids(tutorial, concept):
    ids = set(concept.related if concept else [])
    llm_links = db.session.query(TutorialLink).filter_by(from_tutorial_id=tutorial.id, kind="llm").all()
    ids |= {link.to_concept_id for link in llm_links}
    ids.discard(tutorial.concept_id)
    return ids


def _referenced_by(tutorial):
    """Tutorials that link TO this concept — either an explicit LLM link or a
    curriculum `related` edge. Computed live rather than a persisted
    'backlink' row, since both sources are already queryable."""
    llm_backlinks = db.session.query(TutorialLink).filter_by(to_concept_id=tutorial.concept_id, kind="llm").all()
    tutorial_ids = {link.from_tutorial_id for link in llm_backlinks if link.from_tutorial_id != tutorial.id}

    all_concepts = db.session.query(Concept).all()
    for concept in all_concepts:
        if concept.id == tutorial.concept_id:
            continue
        if tutorial.concept_id in (concept.related_json or []):
            other = db.session.query(Tutorial).filter_by(concept_id=concept.id).one_or_none()
            if other:
                tutorial_ids.add(other.id)

    if not tutorial_ids:
        return []
    return db.session.query(Tutorial).filter(Tutorial.id.in_(tutorial_ids)).all()


@bp.get("/tutorials/<slug>")
@login_required
def view(slug):
    tutorial = db.session.query(Tutorial).filter_by(slug=slug).one_or_none()
    if tutorial is None:
        abort(404)

    read = db.session.query(TutorialRead).filter_by(user_id=current_user.id, tutorial_id=tutorial.id).one_or_none()
    if read is None:
        abort(404)  # not assigned to this student

    if read.read_at is None:
        read.read_at = datetime.utcnow()
        db.session.commit()

    concept = db.session.get(Concept, tutorial.concept_id)

    # The per-user "why you're seeing this" panel — rendered live from THIS
    # viewer's own history, not baked into the shared body_md. Docs §5.4/§6.3:
    # this is what keeps a shared tutorial personal for every student who
    # lands on it, not just the one whose miss originally generated it.
    my_context = None
    my_event = (
        db.session.query(ConceptEvent)
        .filter_by(user_id=current_user.id, concept_id=tutorial.concept_id, status="missed")
        .order_by(ConceptEvent.created_at.desc())
        .first()
    )
    if my_event:
        attempt = db.session.get(Attempt, my_event.attempt_id)
        if attempt:
            scenario = db.session.get(Scenario, attempt.scenario_id)
            my_context = {"scenario_title": scenario.title if scenario else "", "answer": attempt.answer_text}

    cited_resource_ids = set(tutorial.cited_resource_ids)

    def resolve_marker(resource_id):
        if resource_id not in cited_resource_ids:
            return None
        resource = get_resource(current_app, resource_id)
        if not resource:
            return None
        href = url_for("resources.redirect_resource", resource_id=resource_id)
        return f'<a href="{href}">{resource["title"]}</a>'

    body_html = render_markdown(tutorial.body_md, resolve_resource_marker=resolve_marker)
    exercise_html = render_markdown(tutorial.exercise_md)

    further_reading = [get_resource(current_app, rid) for rid in tutorial.reading_order]
    further_reading = [r for r in further_reading if r]

    related_ids = _related_concept_ids(tutorial, concept)
    related_info = []
    for cid in sorted(related_ids):
        rc = db.session.get(Concept, cid)
        if rc is None:
            continue
        rt = db.session.query(Tutorial).filter_by(concept_id=cid).one_or_none()
        related_info.append({"concept": rc, "tutorial": rt})

    referenced_by = _referenced_by(tutorial)

    return render_template(
        "tutorials/view.html",
        tutorial=tutorial,
        concept=concept,
        read=read,
        my_context=my_context,
        body_html=body_html,
        exercise_html=exercise_html,
        further_reading=further_reading,
        related_info=related_info,
        referenced_by=referenced_by,
    )


@bp.get("/api/tutorials/graph")
@login_required
def graph():
    reads = db.session.query(TutorialRead).filter_by(user_id=current_user.id).all()
    tutorial_ids = {r.tutorial_id for r in reads}
    tutorials = db.session.query(Tutorial).filter(Tutorial.id.in_(tutorial_ids)).all() if tutorial_ids else []

    nodes = [{"id": t.id, "slug": t.slug, "title": t.title, "concept_id": t.concept_id} for t in tutorials]
    by_concept = {t.concept_id: t.id for t in tutorials}

    edges = []
    for t in tutorials:
        concept = db.session.get(Concept, t.concept_id)
        related = _related_concept_ids(t, concept)
        for cid in related:
            if cid in by_concept:
                edges.append({"from": t.id, "to": by_concept[cid]})

    return jsonify({"nodes": nodes, "edges": edges})
