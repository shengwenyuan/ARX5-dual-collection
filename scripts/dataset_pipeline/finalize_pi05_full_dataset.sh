#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 7 ]]; then
  echo "usage: $0 CODE_ROOT WORK_ROOT DATE_ROOT DATASET_NAME REPO_ID EXPECTED_TASK EXPORT_CONTAINER" >&2
  exit 2
fi

code_root=$1
work_root=$2
date_root=$3
dataset_name=$4
repo_id=$5
expected_task=$6
export_container=$7

staged_dataset="${work_root}/lerobot_stacking_five_paper_cups_pi05_v1"
staged_home="${work_root}/lerobot"
final_home="${date_root}/lerobot"
final_dataset="${final_home}/local/${dataset_name}"

while docker ps --format '{{.Names}}' | grep -Fxq "${export_container}"; do
  sleep 60
done

if [[ ! -d "${staged_dataset}" ]]; then
  echo "export ended without committed dataset: ${staged_dataset}" >&2
  exit 1
fi

# The exporter runs in Docker and can leave the committed dataset owned by
# root. Normalize ownership before the host-side atomic moves below.
host_uid=$(id -u)
host_gid=$(id -g)
docker run --rm \
  -v "${work_root}:/work" \
  arx5-dual-collection:dataset \
  sh -c 'chown -R "$1:$2" "$3"' sh \
  "${host_uid}" \
  "${host_gid}" \
  "/work/$(basename "${staged_dataset}")"

docker run --rm \
  -v "${code_root}:/workspace:ro" \
  -v "${work_root}:/work:ro" \
  -w /workspace \
  arx5-dual-collection:dataset \
  sh -c 'PYTHONPATH=/workspace/src:$PYTHONPATH arx5-dataset validate-pi05 "$@"' sh \
  --dataset-root "/work/$(basename "${staged_dataset}")" \
  --repo-id "${repo_id}" \
  --expected-task "${expected_task}"

if [[ -e "${staged_home}" || -e "${final_home}" ]]; then
  echo "refusing to overwrite staged or final LeRobot home" >&2
  exit 1
fi
mkdir -p "${staged_home}/local"
mv "${staged_dataset}" "${staged_home}/local/${dataset_name}"
cp "${work_root}/reports/conversion.json" "${staged_home}/conversion.${dataset_name}.json"

mv "${staged_home}" "${final_home}"
echo "final dataset ready: ${final_dataset}"
