"""Loopback-only Flask review and operations dashboard."""
from __future__ import annotations

import json
import secrets
from typing import Any

from .config import PACKAGE_DIR
from .copy_engine import validate_stored_message
from .db import Database, StateError
from .linkedin_delivery import add_manual_post, confirm_manual_post
from .scheduling import release_messages
from .util import compliance_email_body, iso, utcnow


def create_app(db: Database | None = None):
    try:
        from flask import Flask, abort, flash, redirect, render_template, request, url_for
    except ImportError as exc:
        raise RuntimeError("Flask is not installed; run automation setup") from exc

    db = db or Database()
    db.initialize()
    app = Flask(__name__, template_folder=str(PACKAGE_DIR / "templates"))
    secret = db.setting("dashboard_secret", "")
    if not secret:
        secret = secrets.token_urlsafe(32)
        db.set_setting("dashboard_secret", secret)
    csrf = db.setting("dashboard_csrf", "")
    if not csrf:
        csrf = secrets.token_urlsafe(32)
        db.set_setting("dashboard_csrf", csrf)
    app.secret_key = secret
    app.config.update(SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_HTTPONLY=True)

    @app.before_request
    def protect_loopback_and_posts():
        if request.remote_addr not in {"127.0.0.1", "::1"}:
            abort(403)
        if request.method == "POST" and not secrets.compare_digest(request.form.get("csrf", ""), csrf):
            abort(403)

    @app.context_processor
    def helpers():
        return {"csrf": csrf, "json_loads": json.loads}

    @app.get("/")
    def index():
        queue = db.rows(
            "SELECT m.*,p.company,p.domain,p.market,p.fit_score,po.post_url,po.author_name "
            "FROM messages m LEFT JOIN prospects p ON p.id=m.prospect_id "
            "LEFT JOIN posts po ON po.id=m.post_id "
            "WHERE m.status IN ('pending_approval','approved','scheduled') "
            "ORDER BY CASE m.status WHEN 'pending_approval' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,m.created_at"
        )
        evidence: dict[int, list[Any]] = {}
        settings = db.settings()
        delivery_previews: dict[int, str] = {}
        for message in queue:
            ids = [x for x in json.loads(message["evidence_ids_json"] or "[]") if x > 0]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                evidence[message["id"]] = db.rows(
                    f"SELECT * FROM evidence WHERE id IN ({placeholders}) ORDER BY id", ids)
            elif message["post_id"]:
                evidence[message["id"]] = []
            if message["channel"] == "email" and str(
                    settings.get("business_postal_address") or "").strip():
                delivery_previews[message["id"]] = compliance_email_body(message["body"], settings)
        channels = {row["channel"]: row for row in db.rows("SELECT * FROM channel_state ORDER BY channel")}
        metrics = {
            row["status"]: row["n"] for row in db.rows(
                "SELECT status,COUNT(*) AS n FROM messages GROUP BY status")
        }
        campaign_counts = db.rows(
            "SELECT c.name,p.status,COUNT(*) AS n FROM prospects p JOIN campaigns c ON c.id=p.campaign_id "
            "GROUP BY c.name,p.status ORDER BY c.name,p.status"
        )
        events = db.rows(
            "SELECT e.*,m.subject,m.to_address FROM delivery_events e "
            "LEFT JOIN messages m ON m.id=e.message_id ORDER BY e.id DESC LIMIT 50"
        )
        email_event_counts = {
            row["event_type"]: int(row["n"])
            for row in db.rows(
                "SELECT event_type,COUNT(DISTINCT message_id) AS n FROM delivery_events "
                "WHERE channel='email' AND message_id IS NOT NULL GROUP BY event_type"
            )
        }
        submitted = email_event_counts.get("sent", 0)
        bounced = email_event_counts.get("bounce", 0)
        email_tracking = {
            "submitted": submitted,
            "no_bounce_observed": max(0, submitted - bounced),
            "bounced": bounced,
            "human_replies": email_event_counts.get("human_reply", 0),
            "automated_replies": email_event_counts.get("automated_reply", 0),
            "opt_outs": email_event_counts.get("opt_out", 0),
        }
        source_runs = db.rows("SELECT * FROM source_runs ORDER BY id DESC LIMIT 30")
        month = iso()[:7]
        spent = db.row("SELECT COALESCE(SUM(cost_usd),0) AS n FROM source_runs "
                       "WHERE substr(started_at,1,7)=?", (month,))["n"]
        return render_template(
            "dashboard.html", queue=queue, evidence=evidence, channels=channels,
            delivery_previews=delivery_previews,
            metrics=metrics, campaign_counts=campaign_counts, events=events,
            email_tracking=email_tracking,
            source_runs=source_runs, source_spend=spent,
            source_budget=db.setting("apify_monthly_budget_usd", 5.0),
            linkedin_posting_mode=settings.get("linkedin_posting_mode", "manual"),
            markets=sorted(settings.get("market_policies", {}).keys()),
        )

    @app.post("/linkedin/manual-posts")
    def manual_post_intake():
        try:
            post_id = add_manual_post(
                db,
                post_url=request.form.get("post_url", ""),
                author_url=request.form.get("author_url", ""),
                author_name=request.form.get("author_name", ""),
                post_text=request.form.get("post_text", ""),
                role=request.form.get("role", ""),
                market=request.form.get("market", ""),
                published_at=request.form.get("published_at", ""),
                prospect_domain=request.form.get("prospect_domain", ""),
            )
            flash(f"Added manual LinkedIn post {post_id}. Run automation prepare to draft comments.")
        except (StateError, ValueError) as exc:
            flash(f"Could not add LinkedIn post: {exc}")
        return redirect(url_for("index"))

    @app.post("/messages/<int:message_id>/edit")
    def edit_message(message_id: int):
        try:
            db.edit_message(message_id, request.form.get("subject", ""), request.form.get("body", ""))
            errors = validate_stored_message(db, message_id)
            flash("Saved; approval was invalidated." + (" Lint: " + "; ".join(errors) if errors else ""))
        except (StateError, ValueError) as exc:
            flash(str(exc))
        return redirect(url_for("index"))

    @app.post("/messages/<int:message_id>/approve")
    def approve(message_id: int):
        errors = validate_stored_message(db, message_id)
        if errors:
            flash("Cannot approve: " + "; ".join(errors))
        else:
            try:
                db.approve_message(message_id)
                flash(f"Approved message {message_id} for today.")
            except StateError as exc:
                flash(str(exc))
        return redirect(url_for("index"))

    @app.post("/messages/<int:message_id>/reject")
    def reject(message_id: int):
        try:
            db.reject_message(message_id, request.form.get("note", ""))
            flash(f"Rejected message {message_id}.")
        except StateError as exc:
            flash(str(exc))
        return redirect(url_for("index"))

    @app.post("/messages/<int:message_id>/release")
    def release(message_id: int):
        row = db.row("SELECT channel FROM messages WHERE id=?", (message_id,))
        if not row:
            abort(404)
        result = release_messages(db, [message_id], row["channel"])
        flash(f"Released: {result['released']}; blocked: {result['blocked']}")
        return redirect(url_for("index"))

    @app.post("/messages/<int:message_id>/manual-posted")
    def manual_posted(message_id: int):
        try:
            external_id = confirm_manual_post(db, message_id)
            flash(f"Recorded manual LinkedIn post {message_id}: {external_id}")
        except StateError as exc:
            flash(f"Could not record manual post: {exc}")
        return redirect(url_for("index"))

    @app.post("/batch/<channel>/<action>")
    def batch(channel: str, action: str):
        if channel not in {"email", "linkedin"} or action not in {"approve", "release", "reject"}:
            abort(404)
        ids = [int(value) for value in request.form.getlist("message_ids") if value.isdigit()]
        errors = []
        if action == "release":
            result = release_messages(db, ids, channel)
            flash(f"Released {len(result['released'])}; blocked {result['blocked']}")
            return redirect(url_for("index"))
        done = 0
        for message_id in ids:
            try:
                if action == "approve":
                    lint = validate_stored_message(db, message_id)
                    if lint:
                        errors.append(f"{message_id}: {'; '.join(lint)}")
                        continue
                    db.approve_message(message_id)
                else:
                    db.reject_message(message_id, "batch rejection")
                done += 1
            except StateError as exc:
                errors.append(f"{message_id}: {exc}")
        flash(f"{action.title()}d {done}. " + " ".join(errors))
        return redirect(url_for("index"))

    @app.post("/channels/<channel>/<action>")
    def channel_action(channel: str, action: str):
        if channel not in {"email", "linkedin"} or action not in {"pause", "resume"}:
            abort(404)
        if action == "resume" and db.channel(channel)["emergency_stop"]:
            flash("Clear the emergency stop before resuming this channel.")
        else:
            db.set_channel(channel, paused=(action == "pause"),
                           reason="paused from dashboard" if action == "pause" else "")
            flash(f"{channel} {action}d.")
        return redirect(url_for("index"))

    @app.post("/emergency/<action>")
    def emergency(action: str):
        if action not in {"stop", "clear"}:
            abort(404)
        for channel in ("email", "linkedin"):
            db.set_channel(channel, paused=True,
                           reason="emergency stop" if action == "stop" else "resume explicitly after clearing",
                           emergency_stop=(action == "stop"))
        flash("Emergency stop active." if action == "stop" else "Emergency stop cleared; channels remain paused.")
        return redirect(url_for("index"))

    return app


def serve(db: Database | None = None, port: int | None = None) -> None:
    db = db or Database()
    app = create_app(db)
    app.run(host="127.0.0.1", port=port or int(db.setting("dashboard_port", 8765)),
            debug=False, use_reloader=False)
