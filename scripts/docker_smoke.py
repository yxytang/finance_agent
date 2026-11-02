"""No-model smoke test for an isolated Compose environment.

Run ``--phase exercise``, restart the API, then run ``--phase verify`` with
the same state file. This intentionally imports synthetic data and documents.
Only the Python standard library is required on the host.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ACCOUNT = "docker-smoke-ci"
CSV = (
    "date,merchant,amount,account,txn_id\n"
    f"2026-10-01,DOCKER SMOKE SHOP,0.10,{ACCOUNT},docker-smoke-a\n"
    f"2026-10-02,DOCKER SMOKE SHOP,0.10,{ACCOUNT},docker-smoke-b\n"
    f"2026-10-02,DOCKER SMOKE REFUND,-0.03,{ACCOUNT},docker-smoke-refund\n"
).encode()
FILENAME = "docker-smoke-ci.md"
DOCUMENT = (
    "# Docker smoke budget policy\n\n"
    "This synthetic financial policy is used only for container verification. "
    "Maintain an emergency reserve and review duplicate charges against the "
    "original statements before changing the monthly household budget.\n"
).encode()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


class Client:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(self, path: str, *, method="GET", body=None, expected=200,
                raw=False, authenticate=True):
        headers = {}
        if self.token and authenticate:
            headers["Authorization"] = "Bearer " + self.token
        if body is not None:
            headers["Content-Type"] = "application/octet-stream"
        request = Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=10) as response:
                status, content = response.status, response.read()
        except HTTPError as error:
            status, content = error.code, error.read()
        # Do not echo response bodies or headers: failures may contain configuration.
        require(status == expected, f"{method} {path}: HTTP {status}, expected {expected}")
        return content if raw else json.loads(content)


def wait_for(check, *, timeout=180, label="condition"):
    deadline = time.monotonic() + timeout
    while True:
        try:
            value = check()
            if value:
                return value
        except (URLError, TimeoutError, ConnectionError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out waiting for {label}")
        time.sleep(1)


def wait_healthy(client: Client):
    def check():
        response = client.request("/api/health")
        return response if response.get("ok") and response.get("persistence") else None
    health = wait_for(check, label="API health")
    require(health["document_worker"] == "external", "Expected a separate document worker")
    if client.token:
        require(health["auth"] is True, "Configured API token was not passed to the container")
        client.request("/api/knowledge", expected=401, authenticate=False)


def check_projection(client: Client):
    query = urlencode({"account": ACCOUNT})
    summary = client.request("/api/finance/summary?" + query)["items"]
    if not summary:
        return False
    require(len(summary) == 1, "Unexpected number of monthly summary rows")
    row = summary[0]
    if row["gross_expense"] != "0.20" or row["credits"] != "0.03" or row["net_expense"] != "0.17":
        return False  # The projection may still be processing this batch.
    findings = client.request("/api/finance/anomalies?" + query)["items"]
    return any(set(item["txn_ids"]) == {"docker-smoke-a", "docker-smoke-b"} for item in findings)


def exercise(client: Client, state_path: Path, *, kafka=False):
    imported = client.request("/api/ledger/import", method="POST", body=CSV, expected=201)
    require(imported["imported_rows"] == 3 and not imported["repeated"],
            "Expected a fresh isolated database for this smoke test")
    repeated = client.request("/api/ledger/import", method="POST", body=CSV, expected=201)
    require(repeated["repeated"] and repeated["batch_id"] == imported["batch_id"],
            "Repeated CSV import was not idempotent")
    job = client.request(f"/api/knowledge/files/{FILENAME}", method="POST", body=DOCUMENT, expected=202)

    def complete():
        current = client.request("/api/jobs/" + job["id"])
        require(current["status"] not in {"failed", "cancelled"}, "Document worker failed the smoke upload")
        return current if current["status"] == "succeeded" else None

    completed = wait_for(complete, label="independent document worker")
    require(completed["result"]["chunks"] > 0, "Document produced no searchable chunks")
    session = client.request("/api/sessions", method="POST")
    if kafka:
        wait_for(lambda: check_projection(client), label="Kafka summary and anomaly projections")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"batch_id": imported["batch_id"], "job_id": job["id"],
                                      "session_id": session["id"], "kafka": kafka}), encoding="utf-8")
    verify(client, state_path)


def verify(client: Client, state_path: Path):
    state = json.loads(state_path.read_text(encoding="utf-8"))
    repeated = client.request("/api/ledger/import", method="POST", body=CSV, expected=201)
    require(repeated["repeated"] and repeated["batch_id"] == state["batch_id"],
            "Imported ledger did not persist")
    job = client.request("/api/jobs/" + state["job_id"])
    require(job["status"] == "succeeded" and job["result"]["chunks"] > 0,
            "Completed document job did not persist")
    inventory = client.request("/api/knowledge")
    require(inventory["indexed"], "Knowledge index is unavailable")
    require(any(item["source"] == "uploads/" + FILENAME and item["chunks"] > 0
                for item in inventory["files"]), "Uploaded document/index is missing from shared volume")
    original = client.request("/api/knowledge/source?" + urlencode({"path": "uploads/" + FILENAME}), raw=True)
    # Markdown ingestion normalizes UTF-8 text/newlines on the server OS.
    require(original.replace(b"\r\n", b"\n") == DOCUMENT, "Original uploaded document changed")
    sessions = client.request("/api/sessions")["sessions"]
    require(any(item["id"] == state["session_id"] for item in sessions), "Session did not persist")
    if state["kafka"]:
        wait_for(lambda: check_projection(client), label="persisted Kafka projections")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("exercise", "verify"), default="exercise")
    parser.add_argument("--state", type=Path, default=Path(".pytest_tmp/docker-smoke.json"))
    parser.add_argument("--base-url", default=os.environ.get("FINANCE_DOCKER_SMOKE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--kafka", action="store_true", help="Also require the optional Kafka projections")
    args = parser.parse_args(argv)
    client = Client(args.base_url, os.environ.get("FINANCE_WEB_TOKEN", ""))
    wait_healthy(client)
    if args.phase == "exercise":
        exercise(client, args.state, kafka=args.kafka)
    else:
        verify(client, args.state)
    print(f"Docker smoke {args.phase} passed (no model API calls)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
