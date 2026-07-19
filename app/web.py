#!/usr/bin/env python3
"""autosnap web UI + JSON API. Served by gunicorn, fronted by Nginx Proxy Manager
for HTTPS. Binds plain HTTP inside the container."""

import os
import secrets
import time
from datetime import timedelta

from flask import (Flask, jsonify, redirect, request, send_from_directory,
                   session)

import autosnap as core

app = Flask(__name__, static_folder=None)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

SECRET_PATH = os.environ.get("AUTOSNAP_SECRET", "/etc/autosnap/secret")


def _load_secret():
    try:
        with open(SECRET_PATH) as f:
            s = f.read().strip()
            if s:
                return s
    except OSError:
        pass
    s = secrets.token_hex(32)
    try:
        os.makedirs(os.path.dirname(SECRET_PATH), exist_ok=True)
        with open(SECRET_PATH, "w") as f:
            f.write(s)
        os.chmod(SECRET_PATH, 0o600)
    except OSError:
        pass
    return s


app.secret_key = _load_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Secure left off so plain-http LAN access to :80 keeps working; TLS is
    # terminated at Nginx Proxy Manager. Flip to True if only served via HTTPS.
    SESSION_COOKIE_SECURE=False,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)

# very small in-memory login throttle: ip -> [timestamps of failures]
_FAILS = {}
_MAX_FAILS = 6
_WINDOW = 300


def _allowlist():
    cfg = core.load_config()
    return cfg.get("auth", {}).get("allowlist", ["root@pam"])


def _throttled(ip):
    now = time.time()
    hits = [t for t in _FAILS.get(ip, []) if now - t < _WINDOW]
    _FAILS[ip] = hits
    return len(hits) >= _MAX_FAILS


def _record_fail(ip):
    _FAILS.setdefault(ip, []).append(time.time())


@app.before_request
def _guard():
    p = request.path
    configured = core.is_configured()
    # first-run setup (open until configured, since login needs a host to validate against)
    if p == "/setup" or p.startswith("/api/setup"):
        if configured and p == "/setup":
            return redirect("/")
        return None
    if not configured:
        if p.startswith("/api/"):
            return jsonify({"error": "setup required"}), 503
        return redirect("/setup")
    # normal auth
    if p == "/login" or p.startswith("/api/login"):
        return None
    if session.get("user"):
        return None
    if p.startswith("/api/"):
        return jsonify({"error": "unauthorized"}), 401
    return redirect("/login")


@app.route("/setup")
def setup_page():
    return send_from_directory(STATIC_DIR, "setup.html")


@app.route("/api/setup", methods=["GET", "POST"])
def api_setup():
    cfg = core.load_config()
    if request.method == "GET":
        host = cfg["settings"].get("pve_host", "")
        return jsonify({
            "configured": core.is_configured(),
            "pve_host": "" if host == "CHANGE_ME" else host,
            "pve_port": cfg["settings"].get("pve_port", 8006),
        })
    if core.is_configured():
        return jsonify({"error": "already configured"}), 409
    body = request.get_json(force=True) or {}
    host = str(body.get("pve_host", "")).strip()
    port = int(body.get("pve_port", 8006) or 8006)
    token = str(body.get("token", "")).strip()
    verify = bool(body.get("verify_tls", False))
    if not host or not token:
        return jsonify({"error": "host and token are required"}), 400
    if not core.check_token({"pve_host": host, "pve_port": port, "verify_tls": verify}, token):
        return jsonify({"error": "token does not work with this host (check address / privileges)"}), 400
    cfg["settings"]["pve_host"] = host
    cfg["settings"]["pve_port"] = port
    cfg["settings"]["verify_tls"] = verify
    core.save_config(cfg)
    with open(core.TOKEN_PATH, "w") as f:
        f.write(token)
    os.chmod(core.TOKEN_PATH, 0o600)
    return jsonify({"ok": True})


@app.route("/login")
def login_page():
    return send_from_directory(STATIC_DIR, "login.html")


@app.route("/api/login", methods=["POST"])
def do_login():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
    if _throttled(ip):
        return jsonify({"error": "too many attempts, please wait a moment"}), 429
    body = request.get_json(force=True) or {}
    username = str(body.get("username", "")).strip()
    realm = str(body.get("realm", "pam")).strip()
    password = str(body.get("password", ""))
    if "@" not in username:
        username = f"{username}@{realm}"
    if username not in _allowlist():
        _record_fail(ip)
        return jsonify({"error": "this user is not allowed to access the panel"}), 403
    cfg = core.load_config()
    if not core.verify_credentials(cfg["settings"], username, password):
        _record_fail(ip)
        return jsonify({"error": "wrong login or password"}), 401
    session.permanent = True
    session["user"] = username
    return jsonify({"ok": True, "user": username})


@app.route("/api/logout", methods=["POST"])
def do_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me")
def me():
    return jsonify({"user": session.get("user")})


def _pve_and_index():
    cfg = core.load_config()
    pve = core.PVE(cfg["settings"])
    return cfg, pve, core.guest_index(pve)


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/presets")
def presets():
    return jsonify(core.PRESETS)


@app.route("/api/guests")
def guests():
    cfg, pve, idx = _pve_and_index()
    state = core.load_state()
    now = int(time.time())
    out = []
    for vmid, meta in sorted(idx.items(), key=lambda kv: int(kv[0])):
        g = core.guest_settings(cfg, vmid)
        st = state.get(vmid, {})
        # snapshot counts
        managed = protected = 0
        try:
            rx = core.snap_regex(g["prefix"])
            for s in pve.list_snapshots(meta["node"], meta["type"], vmid):
                n = s.get("name", "")
                if n == "current":
                    continue
                if rx.match(n):
                    managed += 1
                else:
                    protected += 1
        except Exception:  # noqa: BLE001
            managed = protected = -1
        next_due = None
        if g["enabled"]:
            if g.get("mode") == "calendar":
                next_due = core.next_occurrence(g, now)
            elif st.get("last_run"):
                next_due = st["last_run"] + int(g["interval_minutes"]) * 60
        out.append({
            "vmid": int(vmid), "name": meta["name"], "type": meta["type"],
            "node": meta["node"], "status": meta["status"],
            "config": g,
            "configured": vmid in cfg.get("guests", {}),
            "managed": managed, "protected": protected,
            "last_run": st.get("last_run"), "last_created": st.get("last_created"),
            "last_deleted": st.get("last_deleted"), "last_error": st.get("last_error"),
            "next_due": next_due, "now": now,
        })
    return jsonify(out)


import re as _re

_ALLOWED = {"enabled", "mode", "interval_minutes", "times", "weekdays",
            "create", "keep", "prefix", "dryrun"}
_HHMM = _re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def _clean_guest(body):
    clean = {}
    for k in _ALLOWED:
        if k not in body:
            continue
        v = body[k]
        if k in ("enabled", "create", "dryrun"):
            clean[k] = bool(v)
        elif k in ("interval_minutes", "keep"):
            clean[k] = max(0, int(v))
        elif k == "mode":
            clean[k] = "calendar" if v == "calendar" else "interval"
        elif k == "times":
            times = [str(t).strip() for t in (v or []) if _HHMM.match(str(t).strip())]
            times = sorted(set(times)) or ["03:00"]
            clean[k] = times
        elif k == "weekdays":
            clean[k] = sorted({int(d) for d in (v or []) if 0 <= int(d) <= 6})
        elif k == "prefix":
            if not _re.match(r"^[A-Za-z][A-Za-z0-9]*$", str(v)):
                raise ValueError("invalid prefix")
            clean[k] = str(v)
    return clean


@app.route("/api/guests/<vmid>", methods=["POST"])
def save_guest(vmid):
    body = request.get_json(force=True) or {}
    try:
        clean = _clean_guest(body)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    cfg = core.load_config()
    cfg.setdefault("guests", {})[str(vmid)] = {**core.GUEST_DEFAULTS, **clean}
    core.save_config(cfg)
    # Apply the new retention right away so the snapshot count reflects `keep`
    # without waiting for the next scheduled tick. Best-effort: a prune failure
    # (e.g. host briefly unreachable) must not fail the save.
    pruned = 0
    try:
        pruned = core.prune_now(vmid)
    except Exception as e:  # noqa: BLE001
        core.log(f"prune-on-save for {vmid} failed: {e}")
    return jsonify({"ok": True, "config": cfg["guests"][str(vmid)], "pruned": pruned})


@app.route("/api/settings", methods=["GET", "POST"])
def settings_route():
    cfg = core.load_config()
    if request.method == "GET":
        return jsonify({
            "paused": bool(cfg["settings"].get("paused", False)),
            "verify_tls": bool(cfg["settings"].get("verify_tls", False)),
            "pve_host": cfg["settings"].get("pve_host"),
            "pve_port": cfg["settings"].get("pve_port"),
            "allowlist": cfg.get("auth", {}).get("allowlist", ["root@pam"]),
            "default_keep": int(cfg["settings"].get("default_keep", 8)),
            "scheduler_minutes": 5,
        })
    body = request.get_json(force=True) or {}
    if "paused" in body:
        cfg["settings"]["paused"] = bool(body["paused"])
    if "verify_tls" in body:
        cfg["settings"]["verify_tls"] = bool(body["verify_tls"])
    if "default_keep" in body:
        cfg["settings"]["default_keep"] = max(0, int(body["default_keep"]))
    if "allowlist" in body and isinstance(body["allowlist"], list):
        al = [str(u).strip() for u in body["allowlist"] if str(u).strip()]
        cfg.setdefault("auth", {})["allowlist"] = al or ["root@pam"]
    core.save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/retention/apply-all", methods=["POST"])
def retention_apply_all():
    """Set every configured guest's keep to the global default and prune now.

    One place to re-flow retention across the whole fleet: writes the global
    default_keep as an explicit per-guest keep on each configured guest, then
    prunes each so counts drop immediately. Prune errors are collected, not
    fatal. Guests never configured are left untouched (use bulk-enable first).
    """
    cfg = core.load_config()
    keep = max(0, int(cfg["settings"].get("default_keep", 8)))
    guests = cfg.get("guests", {})
    for gc in guests.values():
        gc["keep"] = keep
    core.save_config(cfg)
    changed = pruned = 0
    errors = []
    for vmid in guests:
        changed += 1
        try:
            pruned += core.prune_now(vmid)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{vmid}: {e}")
            core.log(f"apply-all prune for {vmid} failed: {e}")
    return jsonify({"ok": True, "keep": keep, "changed": changed,
                    "pruned": pruned, "errors": errors})


@app.route("/api/snapshots/create-all", methods=["POST"])
def snapshots_create_all():
    """Take a snapshot NOW on every guest whose schedule is enabled.

    Forces creation regardless of the per-guest 'create' toggle, then applies
    that guest's retention (via run_guest). Only enabled guests are touched.
    """
    cfg = core.load_config()
    guests = cfg.get("guests", {})
    targets = [v for v, g in guests.items() if g.get("enabled")]
    created = 0
    errors = []
    for vmid in targets:
        try:
            if core.run_guest(vmid, snapshot=True).get("created"):
                created += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{vmid}: {e}")
            core.log(f"create-all for {vmid} failed: {e}")
    return jsonify({"ok": True, "guests": len(targets),
                    "created": created, "errors": errors})


@app.route("/api/snapshots/delete-all", methods=["POST"])
def snapshots_delete_all():
    """Delete EVERY managed (prefix_) snapshot on all configured guests.

    Destructive but bounded: only autosnap-managed names are removed; manual
    snapshots are never touched (same regex safety as retention).
    """
    cfg = core.load_config()
    guests = cfg.get("guests", {})
    deleted = 0
    errors = []
    for vmid in guests:
        try:
            deleted += core.purge_snapshots(vmid)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{vmid}: {e}")
            core.log(f"delete-all for {vmid} failed: {e}")
    return jsonify({"ok": True, "deleted": deleted, "errors": errors})


@app.route("/api/schedule/apply-all", methods=["POST"])
def schedule_apply_all():
    """Overwrite the schedule (interval OR calendar) on every configured guest.

    Only touches scheduling fields; enabled/create/keep/prefix are left as-is.
    Does not reset last_run, so next-due recomputes from the new schedule.
    """
    body = request.get_json(force=True) or {}
    mode = "calendar" if body.get("mode") == "calendar" else "interval"
    patch = {"mode": mode}
    if mode == "interval":
        patch["interval_minutes"] = max(1, int(body.get("interval_minutes", 360)))
    else:
        times = [str(t).strip() for t in (body.get("times") or []) if _HHMM.match(str(t).strip())]
        patch["times"] = sorted(set(times)) or ["03:00"]
        patch["weekdays"] = sorted({int(d) for d in (body.get("weekdays") or []) if 0 <= int(d) <= 6})
    cfg = core.load_config()
    guests = cfg.get("guests", {})
    for gc in guests.values():
        gc.update(patch)
    core.save_config(cfg)
    return jsonify({"ok": True, "changed": len(guests), "mode": mode})


@app.route("/api/bulk", methods=["POST"])
def bulk():
    """Enable/disable scheduling for every guest at once.

    'enable' applies to ALL guests visible via the API (creating a default
    config for any not configured yet), so one click really flips every row on;
    the user can then toggle individual guests off. 'disable' turns off every
    configured guest.
    """
    body = request.get_json(force=True) or {}
    action = body.get("action")
    if action not in ("enable", "disable"):
        return jsonify({"error": "action must be enable|disable"}), 400
    cfg = core.load_config()
    guests = cfg.setdefault("guests", {})
    n = 0
    if action == "enable":
        pve = core.PVE(cfg["settings"])
        dk = int(cfg["settings"].get("default_keep", core.GUEST_DEFAULTS["keep"]))
        for vmid in core.guest_index(pve):
            prev = guests.get(vmid, {})
            entry = {**core.GUEST_DEFAULTS, **prev}
            entry["enabled"] = True
            if "keep" not in prev:  # honour the global default for freshly-enabled guests
                entry["keep"] = dk
            guests[vmid] = entry
            n += 1
    else:
        for gc in guests.values():
            gc["enabled"] = False
            n += 1
    core.save_config(cfg)
    return jsonify({"ok": True, "changed": n})


@app.route("/api/guests/<vmid>", methods=["DELETE"])
def delete_guest(vmid):
    cfg = core.load_config()
    cfg.get("guests", {}).pop(str(vmid), None)
    core.save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/guests/<vmid>/run", methods=["POST"])
def run_now(vmid):
    try:
        return jsonify({"ok": True, "result": core.run_guest(vmid)})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/guests/<vmid>/snapshot", methods=["POST"])
def snapshot_now(vmid):
    try:
        return jsonify({"ok": True, "result": core.run_guest(vmid, snapshot=True)})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/guests/<vmid>/snapshots")
def guest_snapshots(vmid):
    cfg, pve, idx = _pve_and_index()
    meta = idx.get(str(vmid))
    if not meta:
        return jsonify({"error": "not found"}), 404
    g = core.guest_settings(cfg, vmid)
    rx = core.snap_regex(g["prefix"])
    snaps = []
    for s in pve.list_snapshots(meta["node"], meta["type"], vmid):
        if s.get("name") == "current":
            continue
        snaps.append({
            "name": s.get("name"), "snaptime": s.get("snaptime", 0),
            "description": s.get("description", ""),
            "managed": bool(rx.match(s.get("name", ""))),
        })
    snaps.sort(key=lambda x: x["snaptime"], reverse=True)
    return jsonify(snaps)


@app.route("/api/log")
def get_log():
    try:
        with open(core.LOG_PATH) as f:
            lines = f.readlines()[-200:]
        return jsonify({"log": "".join(lines)})
    except OSError:
        return jsonify({"log": ""})


@app.route("/api/health")
def health():
    try:
        cfg = core.load_config()
        pve = core.PVE(cfg["settings"])
        n = len(pve.resources())
        return jsonify({"ok": True, "guests_visible": n})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
