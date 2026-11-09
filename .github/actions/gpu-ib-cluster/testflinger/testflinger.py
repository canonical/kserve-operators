"""
A module to help with Testflinger deployments.
Copyright 2025 Canonical Ltd. All rights reserved.
"""

import json
import logging
import random
import re
import subprocess
import time
from typing import Any

import yaml

# Think of agents and jobs as JSON objects
Agent = dict[str, Any]
Job = dict[str, Any]
FlexibleQueuesJob = dict[str, Any]

logger = logging.getLogger(__name__)

# Returned by get_job_status when the server cannot report the job state.
UNKNOWN_JOB_STATE = "unknown"
# Printed (with exit code 0) by `testflinger-cli status` for an "unknown" state.
_UNKNOWN_STATE_MESSAGE = "Unable to retrieve job state from the server"
# CLI failures worth retrying for idempotent commands: network or server-side.
_TRANSIENT_ERROR_REGEX = re.compile(
    r"try again later|timed? ?out|connection|temporar|name resolution"
    r"|max retries|service unavailable|bad gateway|\b50[0234]\b",
    re.IGNORECASE,
)


def cancel_jobs(job_ids: list[str], *, check: bool = False) -> None:
    """
    Cancel jobs sequentially using the CLI.

    With ``check=True``, stop on the first CLI failure. The cluster lifecycle
    calls this for individual IDs so it can attempt every cancellation and
    distinguish a failed handoff from a failure during best-effort cleanup.

    :param job_ids: The list of job IDs to cancel
    :param check: Raise on CLI failure instead of only logging it
    """

    logger.info("Cancelling jobs")
    for job_id in job_ids:
        logger.debug(f"Cancelling job: {job_id}")

        # The CLI just outputs an empty string when successfully cancelling a job
        _cli(["cancel", job_id], check=check, retry_transient=True)


def create_job(
    job_config_path: str,
    queue: str | None = None,
    series: str | None = None,
) -> Job:
    """
    Load the job config from the provided path and update the queue and series,
    if specified.

    :param job_config_path: The path to the YAML describing the Testflinger job
    :param queue:
        The queue to submit the job to.
        Overwrites what is in the job config if specified.
    :param series:
        The series to provision the Testflinger agents with.
        Overwrites what is in the job config if specified.
    :returns: A dict defining a Testflinger job
    """

    with open(job_config_path, "r") as file:
        job = yaml.safe_load(file)
    if queue is not None:
        job["job_queue"] = queue
    if "provision_data" not in job:
        job["provision_data"] = {}
    if series is not None:
        job["provision_data"]["distro"] = series

    return job


def create_flexible_queues_job(
    job_config_path: str,
    queues: list[str],
    series: str | None = None,
) -> FlexibleQueuesJob:
    """
    Uses `create_job` to create a job, but adds additional `queues` that may run it.
    These jobs are currently only usable by `submit_jobs_custom_priority`.

    :param job_config_path: The path to the YAML describing the Testflinger job
    :param queues:
        The queues to submit the job to.
        Prepended to what is in the job config.
        Must end up with least one queue.
    :param series:
        The series to provision the Testflinger agents with.
        Overwrites what is in the job config if specified.
    :returns: A dict defining a Multi-queue Testflinger job
    """
    job = create_job(job_config_path, series=series)
    if (default_queue := job.get("job_queue")) is None and not queues:
        # let's raise early instead of while submitting jobs, but that one will raise too
        raise ValueError(
            f"no queues defined either in base_config '{job_config_path}' or provided"
        )
    job["job_queue"] = queues + [] if default_queue is None else [default_queue]
    return job


def get_agents(names: list[str]) -> list[Agent]:
    """
    Get the status of each provided Testflinger agent using the CLI.
    Counterintuitively, the `agent-status` command returns more than just the status
    when called with the `--json` flag,
    which instead returns the full agent data available from the API.

    :param names: The names of the agents whose data is desired
    """

    agents: list[Agent] = []
    for name in names:
        agent = _cli(["agent-status", "--json", name], retry_transient=True)

        # The CLI renames some keys for whatever reason:
        #   "name" in API --> "agent" in CLI
        #   "state" in API --> "status" in CLI
        # For consistency within this module, we restore the "name" key,
        # and because we restore the "name" key, we restore the "state" key
        agent = json.loads(agent)
        agent["name"] = agent.pop("agent")
        agent["state"] = agent.pop("status")
        agents.append(agent)

    return agents


def get_deployed_job_data(
    job_ids: list[str],
    wait_for_ssh: bool = False,
    wait_period_seconds: int = 30,
    wait_timeout_seconds: int = 300,
) -> list[dict[str, str]]:
    """
    Get agent, Ubuntu series, username, and IP address
    for the machines acquired by the provided jobs.
    The username and IP address become available
    for machines in the reserve phase.

    :param job_ids: The jobs to poll for agent data
    :returns:
        A list of dicts containing the job ID, agent, series, username, and IP address
        for each input job
    """

    IP_REGEX = r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}"

    data: list[dict[str, str]] = []
    for job_id in job_ids:
        agent = ""
        series = ""
        username = ""
        ip_address = ""
        output = poll(job_id, oneshot=True)

        rmatch = re.search(
            r"Starting node (?P<agent>[A-Za-z0-9-]+) with distro (?P<series>\w+)",
            output,
        )
        if rmatch is not None:
            agent = rmatch.group("agent")
            series = rmatch.group("series")

        rmatch = re.search(
            f"You can now connect to (?P<username>.*)@(?P<ip_address>{IP_REGEX})",
            output,
        )
        if rmatch is not None:
            username = rmatch.group("username")
            ip_address = rmatch.group("ip_address")

        # There may be a delay between the job reaching the reserve phase
        # and the SSH info becoming available via the poll command.
        # In that case, we wait here until the info is available or we time out.
        if wait_for_ssh and rmatch is None:
            logger.info(
                f"Waiting up to {wait_timeout_seconds} seconds for SSH info for job {job_id}"
            )
            time_start = time.time()

            # We do an initial wait first, since we have already polled once above
            time.sleep(wait_period_seconds)
            while True:
                if time.time() - time_start > wait_timeout_seconds:
                    logger.info(f"Timed out waiting for SSH info for job {job_id}")
                    break
                output = poll(job_id, oneshot=True)
                rmatch = re.search(
                    f"You can now connect to (?P<username>.*)@(?P<ip_address>{IP_REGEX})",
                    output,
                )
                if rmatch is not None:
                    username = rmatch.group("username")
                    ip_address = rmatch.group("ip_address")
                    break
                logger.info(
                    f"SSH info not yet available for job {job_id}, "
                    f"waiting {wait_period_seconds} seconds"
                )
                time.sleep(wait_period_seconds)

        data.append(
            {
                "job_id": job_id,
                "agent": agent,
                "series": series,
                "username": username,
                "IP": ip_address,
            }
        )

    return data


def get_job_status(job_id: str) -> str:
    """
    Use the Testflinger CLI to get the status of a job

    :param job_id: The job whose status to fetch
    :returns: The job status, or UNKNOWN_JOB_STATE if the server cannot tell
    """

    status = _cli(["status", job_id], retry_transient=True)
    if status.startswith(_UNKNOWN_STATE_MESSAGE):
        return UNKNOWN_JOB_STATE
    return status


def get_queue_status(queue: str) -> list[dict[str, str]]:
    """
    Use the Testflinger CLI to get the names of the agents in the specified queue

    :param queue: The queue to query for agent membership
    :returns: A list of agent names
    """

    # The standard output does not contain the necessary information, but the JSON output does
    result = _cli(["queue-status", "--json", queue], retry_transient=True)

    # The output from the CLI has the following structure:
    # {
    #   "queue": <queue>,
    #   "agents": [{"name": <name>, "status": <status>}],
    #   "jobs_waiting": [],
    # }
    return json.loads(result).get("agents", [])


def poll(job_id: str, oneshot: bool = True) -> str:
    """
    Use the Testflinger CLI to poll a job

    :param job_id: The job to poll
    :param oneshot:
        If true, the poll exits immediately,
        rather than running continuously
    :returns: The poll output
    """

    command = ["poll", job_id]
    if oneshot:
        command += ["--oneshot"]
    return _cli(command, retry_transient=True)


def prioritize_agents(agents: list[Agent]) -> list[Agent]:
    """
    Prioritize a list of agents based on their provision history.
    Agents with a failure streak are ranked such that
    the largest failure streak has the lowest priority.
    Agents with a success streak are shuffled
    in order to spread jobs across working machines somewhat evenly.
    Agents without streak data are treated as having an infinite streak of failures.

    :param agents: The agents to prioritize
    :returns: A list of agents in order of their prioritization
    """

    passed: list[Agent] = []
    failed: list[Agent] = []

    for agent in agents:
        # Testflinger agents usually have a "provision_streak_type" of "pass" or "fail",
        if agent.get("provision_streak_type") == "pass":
            passed.append(agent)
        else:
            # includes agents without streak data
            failed.append(agent)

    # Testflinger agents usually have a "provision_streak_count",
    # which is how many times in a row the provisioning has resulted
    # in the same "provision_streak_type".
    # Thus, after sorting, the first entry has the fewest failures
    # If they don't have streak data, treat them as having an infinite failure streak
    failed = sorted(
        failed, key=lambda agent: agent.get("provision_streak_count", float("inf"))
    )

    # Shuffle passing agents to more evenly distribute jobs across them
    random.shuffle(passed)

    return passed + failed


def submit_jobs_custom_priority(
    job: Job | FlexibleQueuesJob,
    num_agents: int = 1,
    attempts: int = 3,
    attempt_wait_seconds: int = 600,
    job_data_filepath: str | None = None,
) -> list[str]:
    """
    Submit a job to a number of agents using our custom prioritization,
    which ignores agents with failing streaks and randomizes among agents with passing streaks

    IMPORTANT:
        This function is **not concurrency-safe**, and calling it in quick succession
        with overlapping target agents may also produce unpredictable results. This is
        because there is an inherent delay between job submission and agent pick-up.

        Wait between successive calls — a good heuristic is to wait until the jobs
        returned by the previous call have reached at least "provision" status
        (see `wait_for_jobs`).

    :param job: The job data to submit to each agent;
        All jobs must have `job_queue` attribute;
        with value of type `str` for normal single-queue job, and
        with value of type `list[str]` for flexible-queues job.
    :param num_agents: The number of agents to request (also the number of jobs to submit)
        Agents will be picked from the queue(s) defined in the `job`.
    :param attempts: How many attempts to make to acquire the full number of agents
    :param attempt_wait_seconds: How many seconds to wait between each attempt
    :param yaml_filepath:
        Where to write the job IDs of successfully submitted jobs;
        data will be appended to existing file.
    :returns: A list of job IDs for successfully submitted jobs
    """

    job_ids: list[str] = []
    num_to_deploy = num_agents

    if (queues := job.get("job_queue")) is None or len(queues) == 0:
        raise ValueError(f"no queues defined: '{job=}'")
    if isinstance(queues, str):  # normal job targeting single queue
        queues = [queues]

    for attempt in range(attempts):
        logger.info(f"Attempt {attempt + 1} of {attempts} to submit jobs to {queues=}")

        # We want to re-query Testflinger on each attempt to get updated statuses
        agents: list[Agent] = []
        for queue in queues:
            agents.extend(get_queue_status(queue))

        # Agents are available to deploy if their status is "waiting"
        #   But we may have duplication across queues, so only take latest status
        status = {agent["name"]: agent["status"] for agent in agents}
        names = [name for name in status if status[name] == "waiting"]
        agents = get_agents(names)
        agents = prioritize_agents(agents)
        logger.debug(f"Found {len(agents)} available agents")

        if not agents:
            logger.warning("No agents available on this attempt")

        # Select the agents we will use, based on priority
        # It's fine if len(agents) < num_agents
        agents = agents[0:num_to_deploy]
        for agent in agents:
            # Because we do our custom agent prioritization,
            # we use the agent queues to make sure we get the agents we want,
            # rather than using the queue directly and having Testflinger choose agents for us.
            # Because all agents use the same job provisioning,
            # we can simply change the queue for each agent,
            # and otherwise re-use the same job data
            job["job_queue"] = agent["name"]
            job_id = submit_job(job)
            if job_id is None:
                logger.error(f"Failed to submit job to {agent['name']}")
                continue
            logger.info(f"Submitted job {job_id} to {agent['name']}")
            job_ids.append(job_id)

        # We write results here in case the
        # GitHub workflow that calls this gets cancelled.
        # That way, the later cleanup step has a record of what to clean up.
        if job_data_filepath is not None:
            write_job_data(
                [{"job_id": job_id} for job_id in job_ids],
                job_data_filepath,
                append=True,  # we might be getting called multiple times
            )

        logger.debug(f"Reserved {len(job_ids)} of {num_agents} machine(s).")

        # No need to wait on the last attempt
        if len(job_ids) == num_agents or attempt + 1 == attempts:
            break
        num_to_deploy = num_agents - len(job_ids)

        logger.info(f"Waiting for {attempt_wait_seconds} seconds")
        time.sleep(attempt_wait_seconds)

    return job_ids


def submit_job(job: Job) -> str | None:
    """
    Submit a job to Testflinger using the CLI

    :param job: The job data to submit
    :returns: The job_id of the job if submission is successful or None otherwise
    """

    if not isinstance(job["job_queue"], str):
        raise ValueError("FlexibleQueueJobs are not supported for direct submission")

    UUID_REGEX = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

    job_id = None
    result = _cli(["submit"], input=json.dumps(job))
    # The output should look like
    # "Job submitted successfully!\njob_id: <uuid>\n"
    if rmatch := re.search(f"job_id: (?P<job_id>{UUID_REGEX})", result):
        job_id = rmatch.group("job_id")

    return job_id


def wait_for_jobs(
    job_ids: list[str],
    status: str,
    polling_period_seconds: int = 60,
    timeout_seconds: int = 1800,
) -> list[str]:
    """
    Wait for jobs to reach a certain status

    :param job_ids: The jobs to wait for
    :param status: The status that indicates a job is ready
    :returns: The job_ids which successfully reached a desired status
    """

    jobs_ready: list[str] = []
    time_start = time.time()
    while True:
        if time.time() - time_start > timeout_seconds:
            logger.info("Timed out waiting for jobs to be ready")
            break
        for job_id in job_ids:
            if job_id in jobs_ready:
                continue
            job_status = get_job_status(job_id).strip()
            logger.debug(f"Job {job_id} has status {job_status}")
            if job_status == status:
                jobs_ready.append(job_id)
        if len(jobs_ready) == len(job_ids):
            break
        logger.info(f"Waiting {polling_period_seconds} seconds for jobs to be ready")
        time.sleep(polling_period_seconds)

    return jobs_ready


def write_job_data(
    data: list[dict[str, Any]], filepath: str, append: bool = True
) -> None:
    """
    A simple wrapper around yaml.dump

    :param data: The agent data to write
    :param filepath: The destination to which the data is written
    :param append:
        Whether to append the data to the file if it exists already instead of
        overrwriting it (mode 'a' vs 'w'); default `True` to avoid data-loss.
    """
    with open(filepath, "a" if append else "w") as file:
        yaml.dump(data, file)


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
    """
    Call the Testflinger CLI.

    Existing callers retain logged failures with empty output. Strict callers
    opt into propagating the subprocess error; empty output alone cannot
    distinguish a successful cancellation from a failed command.

    :param command: The action to provide to the CLI, e.g. submit or cancel
    :param input: A value to pass to the subprocess stdin
    :param check: Propagate command failures after logging them
    :param retries: Number of times to retry on auth time-outs (we always attempt once)
    :param retry_transient:
        Also retry, with exponential backoff, on network/server errors and on an
        unknown job state. Only safe for idempotent commands (not submit).
    :param backoff_seconds: Initial wait between transient retries
    :param max_backoff_seconds: Upper bound of the wait between transient retries
    :returns: The output of the CLI command
    """

    output = ""
    command = ["testflinger-cli"] + command
    if input:
        # The `-` tells the CLI to read from stdin
        command += ["-"]

    attempts = max(retries, 0) + 1  # at least one attempt
    delay = backoff_seconds
    while attempts:
        attempts -= 1
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=True,
                input=input,
                text=True,
            )
            if (
                retry_transient
                and attempts
                and result.stdout.startswith(_UNKNOWN_STATE_MESSAGE)
            ):
                logger.warning(
                    f"Transient output from {command}, retrying in {delay}s: "
                    f"{result.stdout.strip()}"
                )
                time.sleep(delay)
                delay = min(delay * 2, max_backoff_seconds)
                continue
            return result.stdout
        except subprocess.CalledProcessError as error:
            logger.error(f"Command failed: {command}")
            logger.error(f"Captured stdout: {error.stdout}")
            logger.error(f"Captured stderr: {error.stderr}")
            stderr = str(error.stderr).lower()
            if attempts and ("timed out" in stderr and "auth" in stderr):
                # auth timeout means nothing reached testflinger server: no dupes
                time.sleep(1)
                continue
            if (
                attempts
                and retry_transient
                and _TRANSIENT_ERROR_REGEX.search(f"{error.stdout}\n{error.stderr}")
            ):
                logger.warning(f"Transient error, retrying in {delay}s")
                time.sleep(delay)
                delay = min(delay * 2, max_backoff_seconds)
                continue
            if check:
                raise
            else:
                return output

    return output
