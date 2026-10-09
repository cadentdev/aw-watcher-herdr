"""Poll herdr's socket API and report terminal focus and agent activity to ActivityWatch."""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from functools import lru_cache

from . import __version__

log = logging.getLogger("aw-watcher-herdr")

CLIENT_NAME = "aw-watcher-herdr"
FOCUS_EVENT_TYPE = "herdr.focus"
AGENTS_EVENT_TYPE = "herdr.agents"
# Agent states worth recording; idle/done/unknown agents are not doing anything.
ACTIVE_AGENT_STATES = {"working", "blocked"}


# --- herdr -----------------------------------------------------------------


def get_snapshot(herdr: str = "herdr", timeout: float = 3.0) -> dict | None:
    """Return herdr's live session snapshot, or None if herdr isn't running."""
    try:
        proc = subprocess.run(
            [herdr, "api", "snapshot"], capture_output=True, text=True, timeout=timeout, check=True
        )
        return json.loads(proc.stdout)["result"]["snapshot"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None


@lru_cache(maxsize=512)
def project_name(cwd: str) -> str:
    """Name of the git repository containing cwd, falling back to the directory name."""
    path = os.path.abspath(cwd)
    while True:
        if os.path.exists(os.path.join(path, ".git")):
            return os.path.basename(path)
        parent = os.path.dirname(path)
        if parent == path:
            return os.path.basename(os.path.abspath(cwd)) or cwd
        path = parent


def _by(items: list | None, key: str) -> dict:
    return {item[key]: item for item in items or [] if key in item}


def _location(snap: dict, pane: dict) -> dict:
    tabs = _by(snap.get("tabs"), "tab_id")
    workspaces = _by(snap.get("workspaces"), "workspace_id")
    cwd = pane.get("foreground_cwd") or pane.get("cwd") or ""
    return {
        "workspace": workspaces.get(pane.get("workspace_id"), {}).get("label", ""),
        "tab": tabs.get(pane.get("tab_id"), {}).get("label", ""),
        "cwd": cwd,
        "project": project_name(cwd) if cwd else "",
    }


def focus_data(snap: dict) -> dict | None:
    """Event data describing the focused pane, or None if nothing is focused."""
    pane_id = snap.get("focused_pane_id")
    pane = _by(snap.get("panes"), "pane_id").get(pane_id)
    if pane is None:
        return None
    data = _location(snap, pane)
    agent = _by(snap.get("agents"), "pane_id").get(pane_id)
    if agent:
        # No agent_status here: it flips constantly and would fragment focus events.
        data["agent"] = agent.get("agent", "")
        data["title"] = agent.get("terminal_title_stripped", "")
    return data


def active_agents(snap: dict) -> dict[str, dict]:
    """Map pane_id -> event data for every agent that is working or blocked."""
    active = {}
    for agent in snap.get("agents") or []:
        status = agent.get("agent_status")
        if status in ACTIVE_AGENT_STATES and "pane_id" in agent:
            data = _location(snap, agent)
            data.update(
                agent=agent.get("agent", ""),
                status=status,
                title=agent.get("terminal_title_stripped", ""),
            )
            active[agent["pane_id"]] = data
    return active


class SpanTracker:
    """Turn polled agent states into complete events, one per continuous state per pane.

    ActivityWatch heartbeats only merge into a bucket's latest event, so several agents
    working at once would fragment into one event per poll. Instead we track spans locally
    and insert each one when it ends, or every max_span seconds so long runs show up promptly.
    """

    def __init__(self, max_span: float = 300.0):
        self.max_span = max_span
        self.open: dict[str, tuple[float, dict]] = {}

    def update(self, now: float, active: dict[str, dict]) -> list[dict]:
        closed = []
        for pane_id, (start, data) in list(self.open.items()):
            if active.get(pane_id) != data or now - start >= self.max_span:
                closed.append(make_event(start, now - start, data))
                del self.open[pane_id]
        for pane_id, data in active.items():
            self.open.setdefault(pane_id, (now, data))
        return closed

    def flush(self, now: float) -> list[dict]:
        return self.update(now, {})


# --- ActivityWatch -----------------------------------------------------------


def make_event(start: float, duration: float, data: dict) -> dict:
    return {
        "timestamp": datetime.fromtimestamp(start, timezone.utc).isoformat(),
        "duration": round(duration, 3),
        "data": data,
    }


class AWClient:
    """Minimal ActivityWatch REST client (stdlib only)."""

    def __init__(self, server: str):
        self.api = server.rstrip("/") + "/api/0"

    def _request(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.api + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None

    def hostname(self) -> str:
        return self._request("GET", "/info").get("hostname") or socket.gethostname()

    def ensure_bucket(self, bucket_id: str, event_type: str, hostname: str) -> None:
        body = {"client": CLIENT_NAME, "type": event_type, "hostname": hostname}
        try:
            self._request("POST", f"/buckets/{bucket_id}", body)
        except urllib.error.HTTPError as e:
            if e.code != 304:  # 304 = bucket already exists
                raise

    def heartbeat(self, bucket_id: str, event: dict, pulsetime: float) -> None:
        self._request("POST", f"/buckets/{bucket_id}/heartbeat?pulsetime={pulsetime}", event)

    def insert(self, bucket_id: str, events: list[dict]) -> None:
        self._request("POST", f"/buckets/{bucket_id}/events", events)


class DryRunClient:
    """Prints what would be sent instead of talking to ActivityWatch."""

    def hostname(self) -> str:
        return socket.gethostname()

    def ensure_bucket(self, bucket_id: str, event_type: str, hostname: str) -> None:
        print(f"bucket {bucket_id} ({event_type})")

    def heartbeat(self, bucket_id: str, event: dict, pulsetime: float) -> None:
        print(f"heartbeat {bucket_id}: {json.dumps(event['data'])}")

    def insert(self, bucket_id: str, events: list[dict]) -> None:
        for event in events:
            print(f"insert {bucket_id}: {json.dumps(event)}")


# --- main loop ---------------------------------------------------------------


def wait_for_server(client: AWClient, retry: float = 10.0) -> str:
    while True:
        try:
            return client.hostname()
        except (urllib.error.URLError, OSError) as e:
            log.info("ActivityWatch not reachable (%s); retrying in %.0fs", e, retry)
            time.sleep(retry)


def run(args: argparse.Namespace) -> None:
    client = DryRunClient() if args.dry_run else AWClient(args.server)
    hostname = client.hostname() if args.dry_run else wait_for_server(client)
    focus_bucket = f"{CLIENT_NAME}_{hostname}"
    agents_bucket = f"{CLIENT_NAME}-agents_{hostname}"
    client.ensure_bucket(focus_bucket, FOCUS_EVENT_TYPE, hostname)
    if args.agents:
        client.ensure_bucket(agents_bucket, AGENTS_EVENT_TYPE, hostname)
    log.info("watching herdr every %ss -> %s", args.poll, focus_bucket)

    tracker = SpanTracker(args.max_span)
    pending: list[dict] = []
    last_poll = time.time()
    while True:
        now = time.time()
        if now - last_poll > args.poll * 3:
            # We were suspended (e.g. the Mac slept): end open spans when we last saw them.
            pending += tracker.flush(last_poll)
        last_poll = now

        snap = get_snapshot(args.herdr)
        if args.agents:
            pending += tracker.update(now, active_agents(snap) if snap else {})
        try:
            data = focus_data(snap) if snap else None
            if data is not None:
                client.heartbeat(focus_bucket, make_event(now, 0, data), args.poll + 1)
            if pending:
                client.insert(agents_bucket, pending)
                pending = []
        except (urllib.error.URLError, OSError) as e:
            log.warning("failed to reach ActivityWatch: %s", e)

        if args.once:
            if args.agents:
                client.insert(agents_bucket, tracker.flush(time.time()))
            return
        time.sleep(max(0.0, args.poll - (time.time() - now)))


def main() -> None:
    parser = argparse.ArgumentParser(prog=CLIENT_NAME, description=__doc__)
    parser.add_argument("--server", default=None, help="ActivityWatch server URL (default: http://127.0.0.1:5600)")
    parser.add_argument("--testing", action="store_true", help="use the ActivityWatch testing server on port 5666")
    parser.add_argument("--poll", type=float, default=5.0, help="seconds between polls (default: 5)")
    parser.add_argument("--max-span", type=float, default=300.0, help="split agent events after this many seconds (default: 300)")
    parser.add_argument("--no-agents", dest="agents", action="store_false", help="don't record agent activity")
    parser.add_argument("--herdr", default="herdr", help="path to the herdr binary (default: herdr on PATH)")
    parser.add_argument("--dry-run", action="store_true", help="print events instead of sending them")
    parser.add_argument("--once", action="store_true", help="poll once and exit (useful with --dry-run)")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args()
    if args.server is None:
        args.server = "http://127.0.0.1:5666" if args.testing else "http://127.0.0.1:5600"
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        run(args)
    except KeyboardInterrupt:
        pass
