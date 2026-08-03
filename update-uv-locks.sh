#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v uv >/dev/null 2>&1; then
  echo "Errore: uv non trovato nel PATH." >&2
  exit 127
fi

if [[ ! -f "${PROJECT_ROOT}/pyproject.toml" ]]; then
  echo "Errore: pyproject.toml non trovato nella root del progetto." >&2
  exit 1
fi

projects=("${PROJECT_ROOT}")
while IFS= read -r pyproject; do
  projects+=("${PROJECT_ROOT}/${pyproject%/pyproject.toml}")
done < <(
  git -C "${PROJECT_ROOT}" ls-files -- "services/**/pyproject.toml"
)

for project in "${projects[@]}"; do
  relative_project="${project#"${PROJECT_ROOT}/"}"
  if [[ "${project}" == "${PROJECT_ROOT}" ]]; then
    relative_project="."
  fi

  echo "Aggiornamento uv.lock: ${relative_project}"
  (
    cd -- "${project}"
    uv lock --upgrade
  )
done

echo "Aggiornati ${#projects[@]} file uv.lock."
