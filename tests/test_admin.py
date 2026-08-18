from datetime import datetime

from app.db import db
from app.models import User, Scenario, Tutorial, TutorialRead, LLMCall
from app.core.analytics import _bucketed_distribution, admin_overview


def _login(client, user_id):
    with client.session_transaction() as session:
        session["_user_id"] = user_id
        session["_fresh"] = True


def _scenario(user_id, n=1):
    for i in range(n):
        db.session.add(
            Scenario(
                user_id=user_id,
                topic_id="research-demonstrator-stack",
                type="concept",
                band=1,
                title=f"Scenario {i}",
                prompt_md="...",
                target_concepts_json=["containerization"],
                model="fake",
                prompt_version="scenario.v1",
            )
        )


def test_bucketed_distribution_splits_into_equal_population_groups():
    # 10 students: one power user (9), everyone else at 0 — the bottom nine
    # deciles should carry none of the total, the top decile all of it.
    counts = [9] + [0] * 9
    buckets = _bucketed_distribution(counts, n_buckets=10)

    assert len(buckets) == 10
    assert sum(b["n_students"] for b in buckets) == 10
    assert sum(b["sum"] for b in buckets) == 9
    assert buckets[-1]["sum"] == 9
    assert buckets[-1]["pct_of_total"] == 100.0
    assert buckets[-1]["cumulative_pct"] == 100.0
    assert buckets[0]["sum"] == 0


def test_bucketed_distribution_handles_no_students():
    assert _bucketed_distribution([]) == []


def test_bucketed_distribution_uses_fewer_buckets_for_a_small_cohort():
    buckets = _bucketed_distribution([1, 2, 3], n_buckets=10)
    assert len(buckets) == 3
    assert sum(b["n_students"] for b in buckets) == 3


def test_admin_route_is_403_for_students_and_instructors(app, client):
    with app.app_context():
        db.session.add(User(id="stu1", email="student@example.com", password_hash="x", role="student"))
        db.session.add(User(id="ins1", email="instructor@example.com", password_hash="x", role="instructor"))
        db.session.commit()

    _login(client, "stu1")
    assert client.get("/admin").status_code == 403

    _login(client, "ins1")
    assert client.get("/admin").status_code == 403


def test_admin_route_shows_cohort_totals_and_usage(app, client):
    with app.app_context():
        db.session.add(User(id="admin1", email="root@example.com", password_hash="x", role="admin"))
        db.session.add(User(id="stu1", email="a@example.com", password_hash="x", role="student"))
        db.session.add(User(id="stu2", email="b@example.com", password_hash="x", role="student"))
        db.session.flush()

        _scenario("stu1", n=3)
        _scenario("stu2", n=1)
        db.session.flush()

        tutorial = Tutorial(
            concept_id="containerization",
            slug="containerization",
            title="Containers",
            body_md="...",
            model="fake",
            prompt_version="tutorial.v1",
        )
        db.session.add(tutorial)
        db.session.flush()
        db.session.add(TutorialRead(user_id="stu1", tutorial_id=tutorial.id, read_at=datetime.utcnow()))
        db.session.add(TutorialRead(user_id="stu2", tutorial_id=tutorial.id))  # assigned, not read

        db.session.add(
            LLMCall(user_id="stu1", purpose="scenario", model="fake", ok=True, prompt_tokens=100, completion_tokens=50, cost_estimate_cents=1.5)
        )
        db.session.add(
            LLMCall(user_id="stu2", purpose="scenario", model="fake", ok=True, prompt_tokens=200, completion_tokens=80, cost_estimate_cents=2.5)
        )
        db.session.commit()

        data = admin_overview()
        assert data["n_students"] == 2
        assert data["n_tutorials"] == 1
        assert data["n_scenarios"] == 4
        assert data["total_calls"] == 2
        assert data["total_prompt"] == 300
        assert data["total_completion"] == 130
        assert data["total_cost"] == 4.0
        # stu1 has 3 scenarios, stu2 has 1 — both counts must show up somewhere
        # across the (up to 2) buckets.
        assert sum(b["sum"] for b in data["scenario_distribution"]) == 4
        assert sum(b["sum"] for b in data["tutorial_distribution"]) == 1

    _login(client, "admin1")
    r = client.get("/admin")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Admin overview" in body
    assert ">2<" in body  # n_students card
