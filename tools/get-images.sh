#!/bin/bash
#
# This script returns list of container images that are managed by this charm and/or its workload
#
# dynamic list
IMAGE_LIST=()
# only scan kserve-controller to avoid retrieving incorrect image from kserve-web-app
# details are in https://github.com/canonical/bundle-kubeflow/issues/674
IMAGE_LIST+=($(find charms/kserve-controller/ -type f -name metadata.yaml -exec yq '.resources | to_entries | .[] | .value | ."upstream-source"' {} \;))
IMAGE_LIST+=($(grep image charms/kserve-controller/src/templates/serving_runtimes_manifests.yaml.j2 | awk '{print $2}' | sort --unique))
IMAGE_LIST+=($(grep image charms/kserve-controller/src/templates/configmap_manifests.yaml.j2 | awk '{print $3}' | sort --unique | sed s/,//g | sed s/\"//g))
printf "%s\n" "${IMAGE_LIST[@]}"

