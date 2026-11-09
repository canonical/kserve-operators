"""Acquire a Testflinger cluster with an optional two-wave reservation barrier.

Copyright 2026 Canonical Ltd. All rights reserved.

Each tag requests a count of distinct agents from its candidate queues. Agents
are selected in tag order, including busy agents, before any jobs are submitted.

By default, each selected agent receives two consecutive jobs. The first jobs
hold their machines until all are simultaneously reserved; cancelling that wave
lets the second jobs start around the same time. This is a best-effort handoff,
not an atomic scheduler operation. Only second-wave jobs form the deployment cluster.
Setting the first-wave timeout to zero skips the holding jobs and submits just
one deployment job per agent, without a synchronized release.

Every accepted job ID is persisted for cleanup, including partial submissions.
Any acquisition failure cancels this attempt's jobs that were not already
released, without submitting replacements.
"""

import argparse
import logging
import subprocess
import time
import typing
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml
from exceptiongroup import ExceptionGroup

import testflinger  # noqa: E402  (sibling module, run as a script)

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(Path(__file__).stem)


AgentsSpec = typing.TypedDict("AgentsSpec", {"queues": list[str], "count": int})
Tag: typing.TypeAlias = str

ClusterSpec: typing.TypeAlias = dict[Tag, AgentsSpec]
ClusterJobDefs: typing.TypeAlias = dict[Tag, tuple[testflinger.FlexibleQueuesJob, int]]
ClusterJobIds: typing.TypeAlias = dict[Tag, list[str]]
AgentJobOverrides: typing.TypeAlias = dict[str, dict[str, typing.Any]]

ClusterMachineData = typing.TypedDict("ClusterMachineData", {"machine_address": str})
ClusterData: typing.TypeAlias = dict[Tag, list[ClusterMachineData]]

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
# Consecutive polls a job may report no/unknown state (server hiccups) before
# the cluster acquisition is abandoned.
MAX_CONSECUTIVE_UNKNOWN_POLLS = 10


def validate_agent_job_overrides(value: typing.Any) -> AgentJobOverrides:
    """Check the {agent: {job field: value}} shape; job fields are not validated."""
    if not isinstance(value, dict):
        raise ValueError("Agent job overrides must be a mapping")
    for agent, fields in value.items():
        # YAML parses unquoted names like 123 or true as non-strings, which
        # would never match a selected agent
        if not isinstance(agent, str):
            raise ValueError(f"Agent name {agent!r} must be a string")
        if not isinstance(fields, dict):
            raise ValueError(f"Overrides for agent {agent!r} must be a mapping")
        # job_queue is how a job targets its agent; overriding it would misroute
        if "job_queue" in fields:
            raise ValueError(f"Overrides for agent {agent!r} cannot change job_queue")
    return value


def apply_job_overrides(
    job: dict[str, typing.Any], overrides: dict[str, typing.Any]
) -> None:
    """Update `job` in place with `overrides`, which win on conflict.

    Mappings merge recursively; any other value (including lists) replaces the
    existing one. A null value deletes the key. `job` must be a per-agent copy.
    """
    for key, value in overrides.items():
        if value is None:
            job.pop(key, None)
        elif isinstance(value, dict):
            current = job.get(key)
            # A mapping over a scalar/absent value starts from {}, so nulls
            # inside it still act as deletions rather than leaking through.
            nested = current if isinstance(current, dict) else {}
            apply_job_overrides(nested, value)
            job[key] = nested
        else:
            job[key] = deepcopy(value)


def create_job_defs_for_cluster(
    cluster_spec: ClusterSpec,
    job_config_path: str,
    series: str,
) -> ClusterJobDefs:
    """Build each tag's job template and requested count without submitting jobs."""
    job_defs = {
        tags: (
            testflinger.create_flexible_queues_job(
                job_config_path, spec["queues"], series
            ),
            spec["count"],
        )
        for tags, spec in cluster_spec.items()
    }
    return job_defs


def _select_agents(cluster_job_defs: ClusterJobDefs) -> dict[Tag, list[str]]:
    """Preflight an ordered, distinct assignment across all tags.

    Selection is greedy: earlier tags keep their preferred agents, and later
    tags cannot reuse them. Failure raises before submission rather than
    backtracking or reserving an incomplete cluster.
    """
    assignments: dict[Tag, list[str]] = {}
    assigned: set[str] = set()
    for tag, (job_def, num_agents) in cluster_job_defs.items():
        if num_agents <= 0:
            logger.warning(
                "Tag %r has invalid agent count %s, skipping", tag, num_agents
            )
            continue

        queues = job_def["job_queue"]
        if isinstance(queues, str):
            queues = [queues]
        # Shared queues can list the same agent; retain discovery order once.
        names = dict.fromkeys(
            agent["name"]
            for queue in queues
            for agent in testflinger.get_queue_status(queue)
            if agent["name"] not in assigned
        )
        candidates = []
        for agent in testflinger.get_agents(list(names)):
            # An agent cleaning up its current job can accept queued work.
            if agent["state"] in ACTIVE_JOB_STATES | {"cleanup"}:
                candidates.append(agent)
            else:
                logger.warning(
                    "Excluding agent %s with unavailable or unknown state %r",
                    agent["name"],
                    agent["state"],
                )
        # Provisioning history ranks candidates, but does not exclude fallbacks.
        selected = testflinger.prioritize_agents(candidates)[:num_agents]
        if len(selected) != num_agents:
            raise ValueError(
                f"Tag {tag!r} needs {num_agents} distinct operational agents; "
                f"only {len(selected)} remain after earlier tags"
            )
        assignments[tag] = [agent["name"] for agent in selected]
        assigned.update(assignments[tag])
        logger.info("Selected agents for %s: %s", tag, assignments[tag])
    if not assigned:
        raise ValueError("Cluster has no agents to reserve")
    return assignments


def _wait_for_reservations(
    job_ids: list[str],
    *,
    deadline: float,
    polling_period_seconds: int,
    queued_job_ids: typing.Sequence[str] = (),
) -> None:
    """Wait for a live reservation barrier before an absolute monotonic deadline.

    Every job in ``job_ids`` must currently be reserved in the same poll.
    ``queued_job_ids`` are monitored for failure but need not be ready yet;
    during first-wave acquisition these are the second-wave jobs.
    """
    unknown_polls: dict[str, int] = {}
    while time.monotonic() < deadline:
        # Never accumulate past successes: a reservation may expire between polls.
        statuses = {
            job_id: testflinger.get_job_status(job_id).strip()
            for job_id in (*job_ids, *queued_job_ids)
        }
        logger.info("Reservation states: %s", statuses)
        for job_id, status in statuses.items():
            if status in ("", testflinger.UNKNOWN_JOB_STATE):
                unknown_polls[job_id] = unknown_polls.get(job_id, 0) + 1
                if unknown_polls[job_id] > MAX_CONSECUTIVE_UNKNOWN_POLLS:
                    raise RuntimeError(
                        f"Job {job_id} state unavailable for "
                        f"{unknown_polls[job_id]} consecutive polls"
                    )
                logger.warning(
                    "Job %s state unavailable (%s/%s), retrying next poll",
                    job_id,
                    unknown_polls[job_id],
                    MAX_CONSECUTIVE_UNKNOWN_POLLS,
                )
                continue
            unknown_polls.pop(job_id, None)
            if status in TERMINAL_JOB_STATES:
                raise RuntimeError(
                    f"Job {job_id} ended before cluster readiness: {status}"
                )
            if status not in ACTIVE_JOB_STATES:
                raise RuntimeError(
                    f"Job {job_id} returned an unknown status: {status!r}"
                )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if all(statuses[job_id] == "reserve" for job_id in job_ids):
            return
        time.sleep(min(polling_period_seconds, remaining))
    raise TimeoutError(f"Deadline exceeded waiting for reservations: {job_ids}")


def _cancel_jobs(job_ids: typing.Iterable[str]) -> set[str]:
    """Attempt every cancellation, log failures, and return their IDs.

    The caller can reject a failed handoff without losing the original error
    when the same helper is used for failure cleanup.
    """
    failed_job_ids: set[str] = set()
    for job_id in job_ids:
        try:
            testflinger.cancel_jobs([job_id], check=True)
        except (subprocess.CalledProcessError, OSError):
            logger.exception("Failed to cancel owned job %s", job_id)
            failed_job_ids.add(job_id)
    return failed_job_ids


def _replace_job_data(data: list[dict[str, typing.Any]], filepath: str) -> None:
    """Keep the old cleanup ledger until the enriched replacement is complete.

    A temporary file beside the destination keeps the rename on one filesystem,
    so a failed write cannot truncate the only persistent record of owned jobs.
    """
    with NamedTemporaryFile(dir=Path(filepath).parent, delete=False) as file:
        temporary = Path(file.name)
    try:
        testflinger.write_job_data(data, str(temporary), append=False)
        temporary.replace(filepath)
    finally:
        temporary.unlink(missing_ok=True)


def deploy_cluster(
    cluster_job_defs: ClusterJobDefs,
    *,
    output_filepath: str,
    cluster_output_filepath: str,
    agent_job_overrides: AgentJobOverrides,
    first_wave_timeout_seconds: int = 21600,
    second_wave_timeout_seconds: int = 2700,
    job_polling_period_seconds: int = 60,
) -> None:
    """Reserve deployment jobs, optionally preceded by a holding wave.

    Independent option errors are raised together as an ExceptionGroup before
    discovery or submission. The first deadline covers submission and barrier
    formation; the second starts at release and includes cancellation time.
    A zero first-wave timeout skips holding jobs: the second timeout then covers
    submission and readiness of a single deployment wave.

    The job-data file owns every submitted ID for cleanup, but only deployment
    records receive SSH details. The cluster-data file contains deployment
    addresses grouped by tag. Failures and interrupts cancel all jobs
    still owned by this attempt, then propagate the original error.

    `agent_job_overrides` maps agent names to job fields applied to every job
    (holding and deployment) submitted to that agent; see
    `apply_job_overrides`. It must come from `validate_agent_job_overrides`.
    """
    errors: list[ValueError] = []
    if first_wave_timeout_seconds < 0:
        errors.append(ValueError("first-wave timeout must be non-negative"))
    for name, value in (
        ("second-wave timeout", second_wave_timeout_seconds),
        ("polling period", job_polling_period_seconds),
    ):
        if value <= 0:
            errors.append(ValueError(f"{name} must be positive"))
    ledger = Path(output_filepath)
    if ledger.resolve() == Path(cluster_output_filepath).resolve():
        errors.append(
            ValueError("Job data and cluster data must use different output paths")
        )
    if ledger.exists() and ledger.stat().st_size:
        errors.append(
            ValueError(f"Job data already exists at {ledger}; use a fresh output path")
        )
    if errors:
        raise ExceptionGroup("Invalid cluster deployment options", errors)

    assignments = _select_agents(cluster_job_defs)
    use_two_waves = first_wave_timeout_seconds > 0
    # Ownership includes any holding jobs; deployment membership stays separate.
    owned_job_data: dict[str, dict[str, typing.Any]] = {}
    first_wave_job_ids: list[str] = []
    deployment_by_tag: ClusterJobIds = {tag: [] for tag in assignments}
    cancelled_job_ids: set[str] = set()
    submission_deadline = time.monotonic() + (
        first_wave_timeout_seconds if use_two_waves else second_wave_timeout_seconds
    )
    waves = ("holding", "deployment") if use_two_waves else ("deployment",)
    logger.info("Acquiring cluster using %s wave(s)", len(waves))
    try:
        for tag, agents in assignments.items():
            for agent in agents:
                # When enabled, queue the holding job right before its deployment job.
                for wave in waves:
                    if time.monotonic() >= submission_deadline:
                        raise TimeoutError(
                            "Reservation deadline exceeded during submission"
                        )
                    job = deepcopy(cluster_job_defs[tag][0])
                    job["job_queue"] = agent
                    apply_job_overrides(job, agent_job_overrides.get(agent, {}))
                    job_id = testflinger.submit_job(job)
                    if not job_id:
                        raise RuntimeError(f"Failed to submit {wave} job to {agent}")
                    if job_id in owned_job_data:
                        raise RuntimeError(
                            f"Submission returned duplicate job ID {job_id}"
                        )
                    owned_job_data[job_id] = {"job_id": job_id}
                    if wave == "holding":
                        first_wave_job_ids.append(job_id)
                    else:
                        deployment_by_tag[tag].append(job_id)
                    # Persist before another submit can fail or we are interrupted.
                    testflinger.write_job_data(
                        [owned_job_data[job_id]], output_filepath, append=True
                    )
                    logger.info(
                        "Submitted %s job %s to %s (%s)", wave, job_id, agent, tag
                    )

        deployment_job_ids = [
            job_id for ids in deployment_by_tag.values() for job_id in ids
        ]
        # Single-wave readiness shares the submission budget; do not restart it.
        deployment_deadline = submission_deadline
        if use_two_waves:
            logger.info("Waiting for the first-wave reservation barrier")
            _wait_for_reservations(
                first_wave_job_ids,
                deadline=submission_deadline,
                polling_period_seconds=job_polling_period_seconds,
                queued_job_ids=deployment_job_ids,
            )
            logger.info("Releasing the first wave")
            # Slow cancellation consumes the handoff budget, rather than extending it.
            deployment_deadline = time.monotonic() + second_wave_timeout_seconds
            failed_job_ids = _cancel_jobs(first_wave_job_ids)
            # Do not cancel successfully released jobs again during failure cleanup.
            cancelled_job_ids = set(first_wave_job_ids) - failed_job_ids
            if failed_job_ids:
                raise RuntimeError(
                    f"Failed to release first-wave jobs: {sorted(failed_job_ids)}"
                )

        logger.info("Waiting for deployment reservations")
        _wait_for_reservations(
            deployment_job_ids,
            deadline=deployment_deadline,
            polling_period_seconds=job_polling_period_seconds,
        )
        # SSH details can lag behind reserve; recheck liveness after fetching them.
        deployed_job_data = testflinger.get_deployed_job_data(
            deployment_job_ids, wait_for_ssh=True
        )
        if {data["job_id"] for data in deployed_job_data} != set(deployment_job_ids):
            raise RuntimeError("Incomplete deployment job data")
        for data in deployed_job_data:
            if not data["username"] or not data["IP"]:
                raise RuntimeError(
                    f"Missing SSH connection details for job {data['job_id']}"
                )
            status = testflinger.get_job_status(data["job_id"]).strip()
            if status != "reserve":
                raise RuntimeError(
                    f"Deployment job {data['job_id']} is no longer reserved: {status!r}"
                )
            owned_job_data[data["job_id"]] = data
        _replace_job_data(list(owned_job_data.values()), output_filepath)
        write_cluster_data(
            deployment_by_tag, deployed_job_data, cluster_output_filepath
        )
        logger.info("Wrote cluster data to %s", cluster_output_filepath)
    except (Exception, KeyboardInterrupt):
        logger.exception("Cluster acquisition failed; cancelling owned jobs")
        _cancel_jobs(
            job_id for job_id in owned_job_data if job_id not in cancelled_job_ids
        )
        raise


def write_cluster_data(
    cluster_job_ids: ClusterJobIds,
    job_data: list[dict[str, typing.Any]],
    output_filepath: str,
) -> None:
    """Write role/address YAML for the supplied deployment jobs, not the ledger."""
    job_data_for_id = {data["job_id"]: data for data in job_data}

    cluster_data: ClusterData = defaultdict(list)
    for tag, job_ids in cluster_job_ids.items():
        for job_id in job_ids:
            job = job_data_for_id[job_id]
            machine_data: ClusterMachineData = {
                "machine_address": f"{job['username']}@{job['IP']}",
            }
            cluster_data[tag].append(machine_data)

    with open(output_filepath, "w") as file:
        # dump the dict form, defaultdict has extra stuff
        yaml.dump(dict(cluster_data), file)


def main():
    """Load the cluster specification and run its reservation lifecycle."""
    args = parse_args()
    agent_job_overrides: AgentJobOverrides = {}
    if args.agent_job_overrides_path is not None:
        logger.info(
            "Loading agent job overrides from %s", args.agent_job_overrides_path
        )
        with open(args.agent_job_overrides_path) as file:
            agent_job_overrides = validate_agent_job_overrides(yaml.safe_load(file))

    logger.info(
        f"Attempting to deploy Testflinger cluster spec '{args.cluster_spec_path}'"
        f"using the job config at '{args.job_config_path}' and '{args.series=}'"
    )

    with open(args.cluster_spec_path, "r") as f:
        cluster_spec = yaml.safe_load(f)
    cluster_job_defs = create_job_defs_for_cluster(
        cluster_spec, args.job_config_path, args.series
    )
    logger.debug(f"{cluster_job_defs=}")

    deploy_cluster(
        cluster_job_defs,
        output_filepath=args.output_filepath,
        cluster_output_filepath=args.cluster_output_filepath,
        agent_job_overrides=agent_job_overrides,
        first_wave_timeout_seconds=args.first_wave_timeout_seconds,
        second_wave_timeout_seconds=args.second_wave_timeout_seconds,
        job_polling_period_seconds=args.job_polling_period_seconds,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse syntax and types; deploy_cluster groups semantic preflight errors."""
    parser = argparse.ArgumentParser(
        description="Reserve a cluster with an optional two-wave barrier"
    )
    parser.add_argument(
        "cluster_spec_path", type=str, help="The path to the cluster spec to provision"
    )
    parser.add_argument(
        "job_config_path", type=str, help="The path to the job data to dispatch"
    )
    _ = parser.add_argument(
        "--agent-job-overrides-path",
        help="Per-agent job overrides YAML; omitted means no overrides",
    )
    parser.add_argument(
        "--cluster-output-filepath",
        required=False,
        default=f"{Path.home()}/testflinger-cluster-data.yaml",
        help="The filepath to write the Testflinger cluster data to",
    )
    parser.add_argument("--series", default="noble", help="Ubuntu series to provision")
    parser.add_argument(
        "--output-filepath",
        default=f"{Path.home()}/testflinger-job-data.yaml",
        help="Cleanup ledger for all jobs, with connection data only for deployment jobs",
    )
    parser.add_argument(
        "--first-wave-timeout-seconds",
        type=int,
        default=21600,
        help=(
            "Deadline for the first-wave barrier; 0 submits a single deployment wave "
            "(default: 21600)"
        ),
    )
    parser.add_argument(
        "--second-wave-timeout-seconds",
        type=int,
        default=2700,
        help=(
            "Deployment readiness deadline from first-wave release, or from submission "
            "in single-wave mode (default: 2700)"
        ),
    )
    parser.add_argument(
        "--job-polling-period-seconds",
        type=int,
        default=60,
        help="Interval between reservation status polls (default: 60)",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    main()
