# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""A thin wrapper around testflinger-cli."""

import json
import logging
import random
import re
import subprocess
import time
from typing import Any

import yaml

Agent = dict[str, Any]
Job = dict[str, Any]

logger = logging.getLogger(__name__)

UNKNOWN_JOB_STATE = "unknown"
# Printed (with exit code 0) by `testflinger-cli status` for an "unknown" state.
_UNKNOWN_STATE_MESSAGE = "Unable to retrieve job state from the server"
# CLI failures worth retrying for idempotent commands: network or server-side.
_TRANSIENT_ERROR_REGEX = re.compile(
    r"try again later|timed? ?out|connection|temporar|name resolution"
    r"|max retries|service unavailable|bad gateway|\b50[0234]\b",
    re.IGNORECASE,
)
_SSH_REGEX = re.compile(
    r"You can now connect to (?P<username>.*)@(?P<ip>[0-9]{1,3}(?:\.[0-9]{1,3}){3})"
)
_JOB_ID_REGEX = re.compile(
    r"job_id: (?P<job_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)


def cancel_job(job_id: str) -> None:
    """Cancel a job; raise on CLI failure."""
    _cli(["cancel", job_id], check=True, retry_transient=True)


def get_agents(names: list[str]) -> list[Agent]:
    """Get the full API data of each agent (`agent-status --json`)."""
    agents: list[Agent] = []
    for name in names:
        agent = _cli_json(["agent-status", "--json", name])
        # The CLI renames name -> agent and state -> status; restore the API keys.
        agent["name"] = agent.pop("agent")
        agent["state"] = agent.pop("status")
        agents.append(agent)
    return agents


def get_queue_status(queue: str) -> list[dict[str, str]]:
    """Get the agents of a queue."""
    return _cli_json(["queue-status", "--json", queue]).get("agents", [])


def get_job_status(job_id: str) -> str:
    """Get the job status, or UNKNOWN_JOB_STATE if the server cannot tell."""
    status = _cli(["status", job_id], retry_transient=True).strip()
    if status.startswith(_UNKNOWN_STATE_MESSAGE):
        return UNKNOWN_JOB_STATE
    return status


def get_ssh_address(
    job_id: str, wait_period_seconds: int = 30, wait_timeout_seconds: int = 300
) -> tuple[str, str] | None:
    """Return (username, IP) from the reserve phase output, waiting for it to show up."""
    deadline = time.monotonic() + wait_timeout_seconds
    while True:
        output = _cli(["poll", job_id, "--oneshot"], retry_transient=True)
        if rmatch := _SSH_REGEX.search(output):
            return rmatch.group("username"), rmatch.group("ip")
        if time.monotonic() >= deadline:
            return None
        logger.info("SSH info not yet available for job %s", job_id)
        time.sleep(wait_period_seconds)


def prioritize_agents(agents: list[Agent]) -> list[Agent]:
    """Order agents: passing provision streaks shuffled first, then fewest failures."""
    passed = [agent for agent in agents if agent.get("provision_streak_type") == "pass"]
    failed = [agent for agent in agents if agent.get("provision_streak_type") != "pass"]
    failed.sort(key=lambda agent: agent.get("provision_streak_count", float("inf")))
    random.shuffle(passed)
    return passed + failed


def submit_job(job: Job) -> str | None:
    """Submit a job; return its ID, or None on failure."""
    # Queue behind busy agents instead of failing when the CLI cannot see an
    # online agent (busy, briefly offline, or a failed agent lookup).
    result = _cli(["submit", "--wait-for-available-agents"], input=json.dumps(job))
    rmatch = _JOB_ID_REGEX.search(result)
    return rmatch.group("job_id") if rmatch else None


def write_yaml(data: Any, filepath: str, append: bool = False) -> None:
    with open(filepath, "a" if append else "w") as file:
        yaml.dump(data, file)


def _cli_json(args: list[str], attempts: int = 5, wait_seconds: int = 30) -> Any:
    """Run a read-only command and parse its JSON output, retrying on garbage output."""
    for attempt in range(1, attempts + 1):
        output = _cli(args, retry_transient=True)
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            if attempt == attempts:
                raise RuntimeError(
                    f"testflinger-cli {' '.join(args)} returned no valid JSON "
                    f"after {attempts} attempts: {output!r}"
                ) from None
            logger.warning(
                "testflinger-cli %s returned no valid JSON (attempt %d/%d), retrying in %ds",
                " ".join(args),
                attempt,
                attempts,
                wait_seconds,
            )
            time.sleep(wait_seconds)


def _cli(
    command: list[str],
    input: str | None = None,
    *,
    check: bool = False,
    retries: int = 10,
    retry_transient: bool = False,
    backoff_seconds: float = 5,
    max_backoff_seconds: float = 60,
) -> str:
    """Call testflinger-cli and return its stdout ("" on failure unless `check`).

    Auth time-outs are always retried (nothing reached the server). With
    `retry_transient`, network/server errors and unknown job states are retried
    with exponential backoff; only safe for idempotent commands (not submit).
    """
    command = ["testflinger-cli", *command]
    if input:
        command.append("-")

    attempts = max(retries, 0) + 1
    delay = backoff_seconds
    while attempts:
        attempts -= 1
        try:
            result = subprocess.run(
                command, capture_output=True, check=True, input=input, text=True
            )
        except subprocess.CalledProcessError as error:
            logger.error("Command failed: %s", command)
            logger.error("Captured stdout: %s", error.stdout)
            logger.error("Captured stderr: %s", error.stderr)
            stderr = str(error.stderr).lower()
            if attempts and "timed out" in stderr and "auth" in stderr:
                time.sleep(1)
                continue
            if (
                attempts
                and retry_transient
                and _TRANSIENT_ERROR_REGEX.search(f"{error.stdout}\n{error.stderr}")
            ):
                logger.warning("Transient error, retrying in %ss", delay)
                time.sleep(delay)
                delay = min(delay * 2, max_backoff_seconds)
                continue
            if check:
                raise
            return ""
        if retry_transient and attempts and result.stdout.startswith(_UNKNOWN_STATE_MESSAGE):
            logger.warning("Transient output from %s, retrying in %ss", command, delay)
            time.sleep(delay)
            delay = min(delay * 2, max_backoff_seconds)
            continue
        return result.stdout
    return ""
