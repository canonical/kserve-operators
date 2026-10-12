#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Deploy an s3-integrator that serves the credentials of the test model's bucket."""

import jubilant

from .charms_dependencies import S3_INTEGRATOR

# Juju user-secret label holding the S3 access/secret keys handed to s3-integrator.
S3_CREDS_JUJU_SECRET_LABEL = "s3-creds"


def deploy_s3_integrator(
    juju: jubilant.Juju,
    model_s3_uri: str,
    endpoint: str,
    region: str,
    access_key: str,
    secret_key: str,
) -> None:
    """Deploy s3-integrator for the model's bucket, passing the keys via a Juju secret."""
    bucket = model_s3_uri.removeprefix("s3://").split("/", 1)[0]
    juju.deploy(
        S3_INTEGRATOR.charm,
        channel=S3_INTEGRATOR.channel,
        config={"endpoint": endpoint, "region": region, "bucket": bucket},
    )
    secret_uri = juju.cli(
        "add-secret",
        S3_CREDS_JUJU_SECRET_LABEL,
        f"access-key={access_key}",
        f"secret-key={secret_key}",
    ).strip()
    juju.cli("grant-secret", S3_CREDS_JUJU_SECRET_LABEL, S3_INTEGRATOR.charm)
    juju.config(S3_INTEGRATOR.charm, {"credentials": secret_uri})
