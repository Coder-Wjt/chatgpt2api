#!/bin/sh
set -eu

seed_root=/opt/chatgpt2api
runtime_root=/app
marker_name=.chatgpt2api-image-build
seed_build="$(tr -d '\r\n' < /opt/chatgpt2api-build-id)"
installed_image_build=""

if [ -f "${runtime_root}/${marker_name}" ]; then
  installed_image_build="$(tr -d '\r\n' < "${runtime_root}/${marker_name}")"
fi

if [ ! -f "${runtime_root}/VERSION" ] || [ "${installed_image_build}" != "${seed_build}" ]; then
  mkdir -p "${runtime_root}"
  find "${runtime_root}" -mindepth 1 -maxdepth 1 \
    ! -name data \
    ! -name config.json \
    ! -name .venv \
    ! -name "${marker_name}" \
    -exec rm -rf -- {} +

  for source in "${seed_root}"/* "${seed_root}"/.[!.]* "${seed_root}"/..?*; do
    [ -e "${source}" ] || continue
    [ "$(basename "${source}")" = ".venv" ] && continue
    cp -a "${source}" "${runtime_root}/"
  done

  marker_tmp="${runtime_root}/${marker_name}.tmp"
  printf '%s\n' "${seed_build}" > "${marker_tmp}"
  mv "${marker_tmp}" "${runtime_root}/${marker_name}"
fi

cd "${runtime_root}"
uv sync --frozen --no-dev --no-install-project
exec "$@"
