#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import tenacity

RETRY_FOR_THREE_MINUTES = tenacity.Retrying(
    wait=tenacity.wait_exponential(multiplier=1, min=2, max=10),
    stop=tenacity.stop_after_delay(180),
    reraise=True,
)

RETRY_FOR_TEN_MINUTES = tenacity.Retrying(
    wait=tenacity.wait_exponential(multiplier=1, min=2, max=20),
    stop=tenacity.stop_after_delay(600),
    reraise=True,
)

RETRY_FOR_THIRTY_MINUTES = tenacity.Retrying(
    wait=tenacity.wait_exponential(multiplier=1, min=2, max=20),
    stop=tenacity.stop_after_delay(1800),
    reraise=True,
)
