import json
import time

import click

from app.db import db


def register_cli(app):
    @app.cli.command("seed-curriculum")
    @click.option("--path", default="curriculum/topics.yaml")
    def seed_curriculum_cmd(path):
        """Load curriculum/topics.yaml into the topics/concepts tables. Idempotent."""
        from app.curriculum import seed_curriculum

        stats = seed_curriculum(path)
        click.echo(f"Seeded curriculum v{stats['version']}: {stats['topics']} topics, {stats['concepts']} concepts.")

    @app.cli.command("recompute-mastery")
    def recompute_mastery_cmd():
        """Rebuild concept_mastery from the concept_events ledger. concept_events
        is the source of truth; this is the escape hatch if the rollup and the
        ledger ever disagree."""
        from app.training.gaps import recompute_all_mastery

        count = recompute_all_mastery()
        click.echo(f"Recomputed mastery for {count} (user, concept) pairs.")

    @app.cli.command("reap-jobs")
    def reap_jobs_cmd():
        """Mark any `running` job left over from a previous process as failed.
        Runs automatically at boot too; exposed here for manual/ops use."""
        from app.jobs.reaper import reap_stale_jobs

        count = reap_stale_jobs()
        click.echo(f"Reaped {count} stale job(s).")

    @app.cli.command("export-user")
    @click.argument("email")
    @click.option("--out", default=None, help="Output path; defaults to <email>-export.json")
    def export_user_cmd(email, out):
        """Dump one user's attempts, mastery, and tutorial reads as JSON — the
        only durability guarantee against atlasflow's ephemeral disk."""
        from app.core.export import export_user_data
        from app.models import User

        user = db.session.query(User).filter_by(email=email.strip().lower()).first()
        if not user:
            raise click.ClickException(f"No user with email {email}")

        data = export_user_data(user)
        out_path = out or f"{email.strip().lower()}-export.json"
        with open(out_path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        click.echo(f"Wrote {out_path}")

    @app.cli.command("promote-instructor")
    @click.argument("email")
    def promote_instructor_cmd(email):
        """Manually flip a user's role to instructor. Normally handled by the
        INSTRUCTOR_EMAILS runtime variable on login; this is for a user who
        registered before being added to that list."""
        from app.models import User

        user = db.session.query(User).filter_by(email=email.strip().lower()).first()
        if not user:
            raise click.ClickException(f"No user with email {email}")
        user.role = "instructor"
        db.session.commit()
        click.echo(f"{email} is now an instructor.")

    @app.cli.command("check-scenario-generation")
    @click.option("--count", default=20, help="How many scenarios to generate back-to-back.")
    @click.option("--email", default="scenario-check@local.test", help="Throwaway user the scenarios are generated for.")
    def check_scenario_generation_cmd(count, email):
        """Generates COUNT scenarios back-to-back against whatever model
        LLM_PROVIDER/LITELLM_MODEL currently resolves to, and reports how many
        fail. The test suite only ever exercises FakeProvider (README ->
        "Running tests" — that's deliberate, so CI never depends on a live
        model), so this is the only place a real model's schema conformance
        under app/llm/client.py's parse-validate-retry-once contract (docs
        §5.1) actually gets measured. Point LLM_PROVIDER/LITELLM_MODEL at the
        backend you want to check before running this."""
        from flask import current_app

        from app.llm.factory import get_provider
        from app.models import User
        from app.training.scenario_service import generate_scenario_for_user

        cfg = current_app.app_config
        if cfg.llm_provider == "fake":
            raise click.ClickException(
                "LLM_PROVIDER=fake never fails; point it at litellm or mistral to check a real model."
            )

        email = email.strip().lower()
        user = db.session.query(User).filter_by(email=email).first()
        if user is None:
            user = User(email=email, password_hash="unusable")
            db.session.add(user)
            db.session.commit()

        provider = get_provider(current_app)
        click.echo(f"Generating {count} scenarios against {cfg.llm_provider}:{provider.model_name}\n")

        failures = []
        for i in range(1, count + 1):
            started = time.monotonic()
            try:
                scenario = generate_scenario_for_user(user, provider)
                click.echo(f"[{i:2d}/{count}] ok     {time.monotonic() - started:5.1f}s  {scenario.title!r}")
            except Exception as exc:  # noqa: BLE001 - one bad attempt must not stop the rest from being tried
                failures.append((i, exc))
                click.echo(f"[{i:2d}/{count}] FAILED {time.monotonic() - started:5.1f}s  {exc}")

        click.echo()
        rate = f" ({len(failures) / count:.0%})" if count else ""
        click.echo(f"{len(failures)}/{count} failed{rate}")
