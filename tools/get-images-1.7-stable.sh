#!/bin/bash
#
# This script returns list of container images that are managed by this charm and/or its workload
#
# static list
STATIC_IMAGE_LIST=(
)
# dynamic list
git checkout origin/track/0.10
IMAGE_LIST=()
IMAGE_LIST+=($(grep image charms/kserve-controller/src/templates/serving_runtimes_manifests.yaml.j2 | awk '{print $2}' | sort --unique))
IMAGE_LIST+=($(grep image charms/kserve-controller/src/templates/configmap_manifests.yaml.j2 | awk '{print $3}' | sort --unique | sed s/,//g | sed s/\"//g))

printf "%s\n" "${STATIC_IMAGE_LIST[@]}"
printf "%s\n" "${IMAGE_LIST[@]}"
