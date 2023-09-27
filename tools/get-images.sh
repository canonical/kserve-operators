#!/bin/bash
#
# This script returns list of container images that are managed by this charm and/or its workload
#
# dynamic list
IMAGE_LIST=()
# only scan kserve-controller to avoid retrieving incorrect image from kserve-web-app
# details are in https://github.com/canonical/bundle-kubeflow/issues/674
IMAGE_LIST+=($(find charms/kserve-controller/ -type f -name metadata.yaml -exec yq '.resources | to_entries | .[] | .value | ."upstream-source"' {} \;))
IMAGE_LIST+=($(yq '.[]' ./charms/kserve-controller/src/default-custom-images.json  | sed 's/"//g'))
printf "%s\n" "${IMAGE_LIST[@]}"

