# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

from charms.resource_dispatcher.v0.resource_dispatcher import KubernetesManifest
from pytest import param

SECRETS_TEST_FILES = ["tests/test_data/secret.yaml.j2"]
SERVICE_ACCOUNTS_TEST_FILES = ["tests/test_data/service-account-yaml.j2"]


MANIFESTS_TEST_DATA = [
    param(
        {
            "secret_name": "test",
            "s3_endpoint": "test",
            "s3_usehttps": "test",
            "s3_region": "test",
            "s3_useanoncredential": "test",
            "s3_access_key": "test",
            "s3_secret_access_key": "test",
        },
        SECRETS_TEST_FILES,
        [
            KubernetesManifest(
                manifest_content='apiVersion: v1\nkind: Secret\nmetadata:\n  name: test\n  annotations:\n     serving.kserve.io/s3-endpoint: "test"\n     serving.kserve.io/s3-usehttps: "test"\n     serving.kserve.io/s3-region: "test"\n     serving.kserve.io/s3-useanoncredential: "test"\ntype: Opaque\nstringData: # use `stringData` for raw credential string or `data` for base64 encoded string\n  AWS_ACCESS_KEY_ID: test\n  AWS_SECRET_ACCESS_KEY: test'
            )
        ],
    ),
    param(
        {
            "svc_account_name": "test",
            "secret_name": "test",
        },
        SERVICE_ACCOUNTS_TEST_FILES,
        [
            KubernetesManifest(
                manifest_content="apiVersion: v1\nkind: ServiceAccount\nmetadata:\n  name: test\nsecrets:\n- name: test"
            )
        ],
    ),
]
