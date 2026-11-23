# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Reserve a Testflinger cluster: one job per agent, each tag with a count of distinct agents.

Every submitted job ID is written to the job data file right away so cleanup can
cancel it. Any failure cancels the submitted jobs and propagates the error.
"""

import argparse
import logging
import subprocess
import time
import typing
from copy import deepcopy
from pathlib import Path

import testflinger
import yaml

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(Path(__file__).stem)

Tag: typing.TypeAlias = str
AgentsSpec = typing.TypedDict("AgentsSpec", {"queues": list[str], "count": int})
ClusterSpec: typing.TypeAlias = dict[Tag, AgentsSpec]
AgentJobOverrides: typing.TypeAlias = dict[str, dict[str, typing.Any]]

# States in which a job can still reach (or remain in) reserve.
ACTIVE_JOB_STATES = {
    "waiting",
    "running",  # Server assignment precedes the agent's first phase update.
    "setup",
    "provision",
    "firmware_update",
    "test",
    "allocate",
    "allocated",
    "reserve",
}
# Cleanup is terminal for acquisition; "complete" is the legacy spelling.
TERMINAL_JOB_STATES = {"cleanup", "cancelled", "completed", "complete"}
# Consecutive polls a job may report an unknown state (server hiccups) before giving up.
MAX_CONSECUTIVE_UNKNOWN_POLLS = 10
POLLING_PERIOD_SECONDS = 60


def load_agent_job_overrides(path: str) -> AgentJobOverrides:
    """Load the {agent: {job field: value}} overrides; job fields are not validated."""
    with open(path) as file:
        value = yaml.safe_load(file)
    if not isinstance(value, dict):
        raise ValueError("Agent job overrides must be a mapping")
    for agent, fields in value.items():
        # YAML parses unquoted names like 123 or true as non-strings.
        if not isinstance(agent, str) or not isinstance(fields, dict):
            raise ValueError(f"Invalid overrides for agent {agent!r}")
        if "job_queue" in fields:
            raise ValueError(f"Overrides for agent {agent!r} cannot change job_queue")
    return value


def select_agents(cluster_spec: ClusterSpec) -> dict[Tag, list[str]]:
    """Greedily pick distinct agents per tag (in tag order) before submitting anything."""
    assignments: dict[Tag, list[str]] = {}
    assigned: set[str] = set()
    for tag, spec in cluster_spec.items():
        # Shared queues can list the same agent; keep discovery order once.
        names = dict.fromkeys(
            agent["name"]
            for queue in spec["queues"]
            for agent in testflinger.get_queue_status(queue)
            if agent["name"] not in assigned
        )
        candidates = []
        for agent in testflinger.get_agents(list(names)):
            # An agent cleaning up its current job can accept queued work.
            if agent["state"] in ACTIVE_JOB_STATES | {"cleanup"}:
                candidates.append(agent)
            else:
                logger.warning("Excluding agent %s in state %r", agent["name"], agent["state"])
        selected = testflinger.prioritize_agents(candidates)[: spec["count"]]
        if len(selected) != spec["count"]:
            raise ValueError(
                f"Tag {tag!r} needs {spec['count']} distinct operational agents; "
                f"only {len(selected)} remain"
            )
        assignments[tag] = [agent["name"] for agent in selected]
        assigned.update(assignments[tag])
        logger.info("Selected agents for %s: %s", tag, assignments[tag])
    if not assigned:
        raise ValueError("Cluster has no agents to reserve")
    return assignments


def wait_for_reservations(job_ids: list[str], deadline: float) -> None:
    """Wait until every job is in reserve in the same poll; fail if any job ends."""
    unknown_polls: dict[str, int] = {}
    while True:
        statuses = {job_id: testflinger.get_job_status(job_id) for job_id in job_ids}
        logger.info("Reservation states: %s", statuses)
        for job_id, status in statuses.items():
            if status in ("", testflinger.UNKNOWN_JOB_STATE):
                unknown_polls[job_id] = unknown_polls.get(job_id, 0) + 1
                if unknown_polls[job_id] > MAX_CONSECUTIVE_UNKNOWN_POLLS:
                    raise RuntimeError(f"Job {job_id} state unavailable for too long")
                continue
            unknown_polls.pop(job_id, None)
            if status in TERMINAL_JOB_STATES:
                raise RuntimeError(f"Job {job_id} ended before cluster readiness: {status}")
            if status not in ACTIVE_JOB_STATES:
                raise RuntimeError(f"Job {job_id} returned an unknown status: {status!r}")
        if all(status == "reserve" for status in statuses.values()):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Deadline exceeded waiting for reservations: {job_ids}")
        time.sleep(min(POLLING_PERIOD_SECONDS, remaining))


def deploy_cluster(
    cluster_spec: ClusterSpec,
    job: testflinger.Job,
    *,
    output_filepath: str,
    cluster_output_filepath: str,
    agent_job_overrides: AgentJobOverrides,
    timeout_seconds: int,
) -> None:
    ledger = Path(output_filepath)
    if ledger.exists() and ledger.stat().st_size:
        raise ValueError(f"Job data already exists at {ledger}; use a fresh output path")

    assignments = select_agents(cluster_spec)
    deadline = time.monotonic() + timeout_seconds
    job_data: dict[str, dict[str, str]] = {}
    jobs_by_tag: dict[Tag, list[str]] = {tag: [] for tag in assignments}
    try:
        for tag, agents in assignments.items():
            for agent in agents:
                agent_job = deepcopy(job)
                agent_job["job_queue"] = agent
                for key, value in agent_job_overrides.get(agent, {}).items():
                    agent_job[key] = deepcopy(value)
                job_id = testflinger.submit_job(agent_job)
                if not job_id:
                    raise RuntimeError(f"Failed to submit the job to {agent}")
                job_data[job_id] = {"job_id": job_id, "agent": agent}
                jobs_by_tag[tag].append(job_id)
                # Persist before another submit can fail or we are interrupted.
                testflinger.write_yaml([job_data[job_id]], output_filepath, append=True)
                logger.info("Submitted job %s to %s (%s)", job_id, agent, tag)

        wait_for_reservations(list(job_data), deadline)
        for job_id, data in job_data.items():
            address = testflinger.get_ssh_address(job_id)
            if address is None:
                raise RuntimeError(f"Missing SSH connection details for job {job_id}")
            data["username"], data["IP"] = address
        testflinger.write_yaml(list(job_data.values()), output_filepath)
        testflinger.write_yaml(
            {
                tag: [
                    {"machine_address": f"{job_data[i]['username']}@{job_data[i]['IP']}"}
                    for i in ids
                ]
                for tag, ids in jobs_by_tag.items()
            },
            cluster_output_filepath,
        )
        logger.info("Wrote cluster data to %s", cluster_output_filepath)
    except (Exception, KeyboardInterrupt):
        logger.exception("Cluster acquisition failed; cancelling the submitted jobs")
        for job_id in job_data:
            try:
                testflinger.cancel_job(job_id)
            except (subprocess.CalledProcessError, OSError):
                logger.exception("Failed to cancel job %s", job_id)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cluster_spec_path", help="Cluster spec: {tag: {queues, count}}")
    parser.add_argument("job_config_path", help="Testflinger job submitted to every agent")
    parser.add_argument("--agent-job-overrides-path", help="Per-agent top-level job fields")
    parser.add_argument("--series", default="noble", help="Ubuntu series to provision")
    parser.add_argument(
        "--timeout-seconds", type=int, default=2700, help="Deadline for all the jobs to be reserved"
    )
    parser.add_argument(
        "--output-filepath",
        required=True,
        help="Job data: IDs (for cleanup), agents and SSH addresses",
    )
    parser.add_argument(
        "--cluster-output-filepath", required=True, help="Machine addresses grouped by tag"
    )
    args = parser.parse_args()

    with open(args.cluster_spec_path) as file:
        cluster_spec = yaml.safe_load(file)
    with open(args.job_config_path) as file:
        job = yaml.safe_load(file)
    job.setdefault("provision_data", {})["distro"] = args.series
    overrides = (
        load_agent_job_overrides(args.agent_job_overrides_path)
        if args.agent_job_overrides_path
        else {}
    )
    deploy_cluster(
        cluster_spec,
        job,
        output_filepath=args.output_filepath,
        cluster_output_filepath=args.cluster_output_filepath,
        agent_job_overrides=overrides,
        timeout_seconds=args.timeout_seconds,
    )


if __name__ == "__main__":
    main()
