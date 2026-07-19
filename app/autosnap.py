#!/usr/bin/env python3
"""autosnap core: scheduled Proxmox snapshots + retention via the PVE REST API.

Runs entirely inside an unprivileged LXC. Talks to the Proxmox host only through
an API token scoped to VM.Snapshot + VM.Audit. The Proxmox host itself is never
modified.

Safety invariants:
  * Managed snapshot names are ALWAYS  <prefix>_YYYYMMDD_HHMMSS
  * Retention deletes ONLY names matching that strict pattern for the configured
    prefix. Manual snapshots (bkp, NOW, current, ...) can never match -> untouched.
  * Every delete re-verifies the name against the regex immediately before the call.
  * dryrun / create / keep are independent switches.
"""

import datetime as dt
import json
import os
import re
import sys
import time

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CONFIG_PATH = os.environ.get("AUTOSNAP_CONFIG", "/etc/autosnap/config.json")
TOKEN_PATH = os.environ.get("AUTOSNAP_TOKEN", "/etc/autosnap/token")
STATE_PATH = os.environ.get("AUTOSNAP_STATE", "/var/lib/autosnap/state.json")
LOG_PATH = os.environ.get("AUTOSNAP_LOG", "/var/log/autosnap/autosnap.log")

DEFAULT_SETTINGS = {
    "pve_host": "172.19.19.1",
    "pve_port": 8006,
    "verify_tls": False,
    "paused": False,          # global master switch: True -> scheduler does nothing
    "default_keep": 8,        # global retention default: seeds new guests + guest_settings fallback
}
DEFAULT_AUTH = {
    "allowlist": ["root@pam"],   # who may log in to the panel
}
GUEST_DEFAULTS = {
    "enabled": False,
    "mode": "interval",          # "interval" | "calendar"
    "interval_minutes": 360,     # interval mode
    "times": ["03:00"],          # calendar mode: HH:MM in container local time
    "weekdays": [],              # calendar mode: [] = codziennie; else subset 0..6 (Mon=0)
    "create": True,
    "keep": 8,
    "prefix": "auto",
    "dryrun": False,
}

PRESETS = {  # label -> minutes, for the UI
    "Every 15 min": 15,
    "Hourly": 60,
    "Every 6 hours": 360,
    "Every 12 hours": 720,
    "Daily": 1440,
    "Weekly": 10080,
}


def log(msg):
    line = f"[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _atomic_write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_config():
    """Read config.json, filling in defaults for missing settings.

    Every key must be carried through: save_config() writes back whatever this
    returns, so anything dropped here is erased from disk on the next save.
    """
    cfg = _read_json(CONFIG_PATH, {})
    settings = dict(DEFAULT_SETTINGS)
    settings.update(cfg.get("settings", {}))
    guests = cfg.get("guests", {})
    auth = dict(DEFAULT_AUTH)
    auth.update(cfg.get("auth", {}))
    return {"settings": settings, "guests": guests, "auth": auth}


def save_config(cfg):
    _atomic_write_json(CONFIG_PATH, cfg)


def guest_settings(cfg, vmid):
    g = dict(GUEST_DEFAULTS)
    stored = cfg.get("guests", {}).get(str(vmid), {})
    # A guest with no explicit keep inherits the global default_keep; once the
    # user sets keep in the guest modal it becomes an explicit per-guest override.
    if "keep" not in stored:
        g["keep"] = int(cfg.get("settings", {}).get("default_keep", GUEST_DEFAULTS["keep"]))
    g.update(stored)
    return g


def load_state():
    return _read_json(STATE_PATH, {})


def save_state(state):
    _atomic_write_json(STATE_PATH, state)


class PVE:
    """Minimal Proxmox REST client using an API token."""

    def __init__(self, settings):
        self.base = f"https://{settings['pve_host']}:{settings['pve_port']}/api2/json"
        self.verify = bool(settings.get("verify_tls", False))
        with open(TOKEN_PATH) as f:
            token = f.read().strip()
        self.headers = {"Authorization": f"PVEAPIToken={token}"}

    def _req(self, method, path, **kw):
        r = requests.request(method, self.base + path, headers=self.headers,
                             verify=self.verify, timeout=30, **kw)
        r.raise_for_status()
        if r.text:
            return r.json().get("data")
        return None

    def resources(self):
        """All guests: list of dicts with vmid, type(lxc/qemu), node, name, status."""
        return self._req("GET", "/cluster/resources?type=vm") or []

    def _kind(self, gtype):
        return "lxc" if gtype == "lxc" else "qemu"

    def list_snapshots(self, node, gtype, vmid):
        path = f"/nodes/{node}/{self._kind(gtype)}/{vmid}/snapshot"
        return self._req("GET", path) or []

    def create_snapshot(self, node, gtype, vmid, name, description=""):
        path = f"/nodes/{node}/{self._kind(gtype)}/{vmid}/snapshot"
        upid = self._req("POST", path, data={"snapname": name, "description": description})
        return self._wait(node, upid)

    def delete_snapshot(self, node, gtype, vmid, name):
        path = f"/nodes/{node}/{self._kind(gtype)}/{vmid}/snapshot/{name}"
        upid = self._req("DELETE", path)
        return self._wait(node, upid)

    def _wait(self, node, upid, timeout=600):
        """Poll a task UPID until it finishes. Returns exitstatus string."""
        if not upid:
            return "OK"
        deadline = time.time() + timeout
        spath = f"/nodes/{node}/tasks/{upid}/status"
        while time.time() < deadline:
            st = self._req("GET", spath)
            if st and st.get("status") == "stopped":
                return st.get("exitstatus", "unknown")
            time.sleep(1.5)
        raise TimeoutError(f"task {upid} did not finish in {timeout}s")


def check_token(settings, token):
    """Return True if the given API token authenticates against the host."""
    url = (f"https://{settings['pve_host']}:{settings['pve_port']}"
           f"/api2/json/version")
    try:
        r = requests.get(url, headers={"Authorization": f"PVEAPIToken={token}"},
                        verify=bool(settings.get("verify_tls", False)), timeout=10)
    except requests.RequestException:
        return False
    return r.status_code == 200


def is_configured():
    """True once a PVE host and an API token have been set (first-run done)."""
    cfg = load_config()
    host = cfg["settings"].get("pve_host", "")
    if not host or host == "CHANGE_ME":
        return False
    try:
        with open(TOKEN_PATH) as f:
            return bool(f.read().strip())
    except OSError:
        return False


def verify_credentials(settings, username, password):
    """Validate a login against Proxmox itself via the ticket API.

    Nothing is stored: the password is checked live against PVE, so whatever
    password is currently valid on the host (e.g. root@pam) is valid here too.
    Returns True only on a successful ticket issue.
    """
    url = (f"https://{settings['pve_host']}:{settings['pve_port']}"
           f"/api2/json/access/ticket")
    try:
        r = requests.post(url, data={"username": username, "password": password},
                          verify=bool(settings.get("verify_tls", False)), timeout=15)
    except requests.RequestException:
        return False
    return r.status_code == 200 and bool((r.json() or {}).get("data", {}).get("ticket"))


def _parse_hhmm(s):
    h, m = str(s).split(":")
    return int(h), int(m)


def next_occurrence(g, now_ts):
    """Next scheduled epoch for calendar mode (None otherwise)."""
    if g.get("mode") != "calendar":
        return None
    times = g.get("times") or []
    weekdays = g.get("weekdays") or []
    if not times:
        return None
    now = dt.datetime.fromtimestamp(now_ts)
    best = None
    for day in range(0, 8):
        d = (now + dt.timedelta(days=day)).date()
        if weekdays and d.weekday() not in weekdays:
            continue
        for t in times:
            try:
                hh, mm = _parse_hhmm(t)
            except ValueError:
                continue
            cand = dt.datetime.combine(d, dt.time(hh, mm)).timestamp()
            if cand > now_ts and (best is None or cand < best):
                best = cand
    return int(best) if best else None


def is_due(g, last_run, now_ts):
    """Whether a guest should run now, given its schedule and last run time."""
    if g.get("mode") == "calendar":
        times = g.get("times") or []
        weekdays = g.get("weekdays") or []
        if not times:
            return False
        # only fire if a scheduled occurrence falls in (window_start, now]
        window_start = last_run if last_run else now_ts - 600
        now = dt.datetime.fromtimestamp(now_ts)
        for day in range(-2, 1):  # look back to catch missed ticks
            d = (now + dt.timedelta(days=day)).date()
            if weekdays and d.weekday() not in weekdays:
                continue
            for t in times:
                try:
                    hh, mm = _parse_hhmm(t)
                except ValueError:
                    continue
                occ = dt.datetime.combine(d, dt.time(hh, mm)).timestamp()
                if window_start < occ <= now_ts:
                    return True
        return False
    interval = int(g.get("interval_minutes", 360)) * 60
    return now_ts - last_run >= interval


def snap_regex(prefix):
    return re.compile(r"^" + re.escape(prefix) + r"_\d{8}_\d{6}$")


def guest_index(pve):
    """Map vmid(str) -> {type, node, name, status} from cluster resources."""
    idx = {}
    for r in pve.resources():
        if r.get("type") not in ("lxc", "qemu"):
            continue
        idx[str(r["vmid"])] = {
            "type": r["type"], "node": r["node"],
            "name": r.get("name", ""), "status": r.get("status", ""),
        }
    return idx


def do_snapshot(pve, meta, vmid, prefix, dryrun):
    name = f"{prefix}_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if dryrun:
        log(f"[DRY-RUN] would create snapshot {name} on {meta['type']}/{vmid}")
        return name
    log(f"creating snapshot {name} on {meta['type']}/{vmid} ({meta['name']})")
    status = pve.create_snapshot(meta["node"], meta["type"], vmid, name,
                                 "autosnap scheduled snapshot")
    if status != "OK":
        raise RuntimeError(f"snapshot task ended with status {status!r}")
    log(f"created {name}")
    return name


def do_prune(pve, meta, vmid, prefix, keep, dryrun):
    keep = int(keep)
    rx = snap_regex(prefix)
    snaps = pve.list_snapshots(meta["node"], meta["type"], vmid)
    managed, protected = [], []
    for s in snaps:
        n = s.get("name", "")
        if n == "current":
            continue
        if rx.match(n):
            managed.append((n, int(s.get("snaptime", 0))))
        else:
            protected.append(n)
    managed.sort(key=lambda x: (x[1], x[0]))
    log(f"guest {vmid}: {len(managed)} managed, protected={protected or 'none'}")
    if keep <= 0:
        log(f"keep={keep} -> retention off, nothing pruned")
        return 0
    if len(managed) <= keep:
        log(f"within retention (keep={keep}), nothing to prune")
        return 0
    to_delete = managed[: len(managed) - keep]
    deleted = 0
    for name, _t in to_delete:
        if not rx.match(name):  # belt-and-suspenders
            log(f"REFUSING to delete non-matching snapshot {name}")
            continue
        if dryrun:
            log(f"[DRY-RUN] would delete {name}")
            continue
        log(f"deleting old snapshot {name}")
        status = pve.delete_snapshot(meta["node"], meta["type"], vmid, name)
        if status != "OK":
            log(f"WARN: delete of {name} ended with status {status!r}")
        else:
            deleted += 1
    return deleted


def run_guest(vmid, force=False, snapshot=None):
    """Process one guest: snapshot (per config/create) + prune. Returns summary dict."""
    cfg = load_config()
    g = guest_settings(cfg, vmid)
    pve = PVE(cfg["settings"])
    idx = guest_index(pve)
    meta = idx.get(str(vmid))
    if not meta:
        raise RuntimeError(f"guest {vmid} not found via API")
    dryrun = bool(g["dryrun"])
    prefix = g["prefix"]
    if not re.match(r"^[A-Za-z][A-Za-z0-9]*$", prefix):
        raise RuntimeError(f"invalid prefix {prefix!r}")
    take = g["create"] if snapshot is None else snapshot
    created = None
    if take:
        created = do_snapshot(pve, meta, vmid, prefix, dryrun)
    deleted = do_prune(pve, meta, vmid, prefix, g["keep"], dryrun)
    state = load_state()
    state[str(vmid)] = {
        "last_run": int(time.time()),
        "last_created": created,
        "last_deleted": deleted,
        "last_error": None,
    }
    save_state(state)
    return {"created": created, "deleted": deleted, "dryrun": dryrun}


def prune_now(vmid):
    """Enforce retention for one guest immediately: prune only, no new snapshot.

    Used when the user changes `keep` in the UI so the snapshot count drops to
    the new value at once instead of waiting for the next scheduled tick. Does
    NOT touch schedule state (last_run), so it never shifts the next-due time.
    Honours the guest's dryrun flag. Returns the number of snapshots deleted.
    """
    cfg = load_config()
    g = guest_settings(cfg, vmid)
    pve = PVE(cfg["settings"])
    meta = guest_index(pve).get(str(vmid))
    if not meta:
        raise RuntimeError(f"guest {vmid} not found via API")
    return do_prune(pve, meta, vmid, g["prefix"], g["keep"], bool(g["dryrun"]))


def purge_snapshots(vmid):
    """Delete EVERY managed (prefix_) snapshot for a guest, ignoring keep.

    Same safety as prune: only names matching ^<prefix>_\\d{8}_\\d{6}$ are touched,
    re-checked immediately before each delete, so manual snapshots (bkp/NOW/…)
    are never removed. Honours the guest's dryrun flag. Returns count deleted.
    """
    cfg = load_config()
    g = guest_settings(cfg, vmid)
    pve = PVE(cfg["settings"])
    meta = guest_index(pve).get(str(vmid))
    if not meta:
        raise RuntimeError(f"guest {vmid} not found via API")
    rx = snap_regex(g["prefix"])
    dryrun = bool(g["dryrun"])
    deleted = 0
    for s in pve.list_snapshots(meta["node"], meta["type"], vmid):
        n = s.get("name", "")
        if n == "current" or not rx.match(n):
            continue
        if dryrun:
            log(f"[DRY-RUN] would delete {n}")
            continue
        if pve.delete_snapshot(meta["node"], meta["type"], vmid, n) == "OK":
            deleted += 1
        else:
            log(f"WARN: delete of {n} did not end OK")
    return deleted


def schedule_tick():
    """Run all enabled guests that are due. Invoked by the systemd timer."""
    cfg = load_config()
    if cfg["settings"].get("paused"):
        log("tick: global pause active, skipping")
        return
    state = load_state()
    now = int(time.time())
    due = []
    for vmid, gc in cfg.get("guests", {}).items():
        g = dict(GUEST_DEFAULTS)
        g.update(gc)
        if not g["enabled"]:
            continue
        last = state.get(str(vmid), {}).get("last_run", 0)
        if is_due(g, last, now):
            due.append(vmid)
    if not due:
        log("tick: nothing due")
        return
    log(f"tick: due guests -> {', '.join(due)}")
    for vmid in due:
        try:
            run_guest(vmid)
        except Exception as e:  # noqa: BLE001 - never let one guest kill the tick
            log(f"ERROR processing guest {vmid}: {e}")
            state = load_state()
            entry = state.get(str(vmid), {})
            entry["last_error"] = str(e)
            entry["last_run"] = now  # avoid hammering a broken guest every tick
            state[str(vmid)] = entry
            save_state(state)


def _cli():
    import argparse
    p = argparse.ArgumentParser(prog="autosnap")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tick", help="run all due guests (scheduler entrypoint)")
    r = sub.add_parser("run", help="process one guest now (snapshot per config + prune)")
    r.add_argument("vmid")
    s = sub.add_parser("snapshot", help="force one snapshot now for a guest")
    s.add_argument("vmid")
    pr = sub.add_parser("prune", help="prune only for a guest")
    pr.add_argument("vmid")
    sub.add_parser("list", help="list guests + config + state as JSON")
    args = p.parse_args()

    if args.cmd == "tick":
        schedule_tick()
    elif args.cmd == "run":
        print(json.dumps(run_guest(args.vmid)))
    elif args.cmd == "snapshot":
        print(json.dumps(run_guest(args.vmid, snapshot=True)))
    elif args.cmd == "prune":
        print(json.dumps(run_guest(args.vmid, snapshot=False)))
    elif args.cmd == "list":
        cfg = load_config()
        pve = PVE(cfg["settings"])
        print(json.dumps(guest_index(pve), indent=2))


if __name__ == "__main__":
    _cli()
