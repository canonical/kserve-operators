#!/bin/bash -x

# Finds the charms in this repo, outputting them as JSON
# Will return one of:
# * the relative paths of the directories listed in `./charms`, if that directory exists
# * "./", if the root directory has a "metadata.yaml" file
# * otherwise, error
#
# Modified from: https://stackoverflow.com/questions/63517732/github-actions-build-matrix-for-lambda-functions/63736071#63736071
CHARMS_DIR="./charms"
if [ -d "$CHARMS_DIR" ];
then
  CHARM_PATHS=$(find $CHARMS_DIR -maxdepth 1 -type d -not -path '*/\.*' -not -path "$CHARMS_DIR")
else
  if [ -f "./metadata.yaml" ]
  then
    CHARM_PATHS="./"
  else
    echo "Cannot find valid charm directories - aborting"
    exit 1
  fi
fi

# Convert output to JSON string format
# { charm_paths: [...] }

# Porting fix: https://github.com/canonical/kserve-operators/commit/1263f31763286dc3155f354d19de6d7f9f1472a8
# which was done on `main` as part of https://github.com/canonical/kserve-operators/pull/141
CHARM_PATHS_LIST=["./charms/kserve-controller"]

# FIXME: kserve-web-app is not ready to be built and published, the publish job for
# that charm is expected to fail. It can be ignored until the charm is not WIP.
echo "Found CHARM_PATHS_LIST: $CHARM_PATHS_LIST (Only publishing kserve-controller)"

echo "::set-output name=CHARM_PATHS_LIST::$CHARM_PATHS_LIST"
