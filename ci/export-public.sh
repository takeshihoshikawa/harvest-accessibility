#!/usr/bin/env bash
# 指定した ref から、公開ミラーへ渡すファイルだけを取り出して dest へ書き出す。
# usage: ci/export-public.sh <git-ref> <dest-dir>
set -euo pipefail

ref="${1:?usage: export-public.sh <git-ref> <dest-dir>}"
dest="${2:?usage: export-public.sh <git-ref> <dest-dir>}"
allowlist="$(cd "$(dirname "$0")" && pwd)/public-files.txt"

rules=()
while IFS= read -r line; do
  case "$line" in '' | '#'*) continue ;; esac
  rules+=("$line")
done < "$allowlist"

files=()
while IFS= read -r f; do
  for r in "${rules[@]}"; do
    if [[ "$r" == */ ]]; then
      [[ "$f" == "$r"* ]] && { files+=("$f"); break; }
    else
      [[ "$f" == "$r" ]] && { files+=("$f"); break; }
    fi
  done
done < <(git ls-tree -r --name-only "$ref")

if [[ ${#files[@]} -eq 0 ]]; then
  echo "公開対象が1件も無い。許可リストと ref を確認する: $ref" >&2
  exit 1
fi

mkdir -p "$dest"
git archive "$ref" -- "${files[@]}" | tar -x -C "$dest"

# ミラーは自分に打たれたタグで Actions を走らせて Release を作るので、
# そのワークフローだけは名前を変えて渡す。作業リポ側では .github/ の下に
# 置かない（置くと作業リポでも Release ができてしまう）。
mkdir -p "$dest/.github/workflows"
git show "$ref:ci/mirror-release.yml" > "$dest/.github/workflows/release.yml"

# 保険。許可リストを書き換えたときに気づけるようにする。
for denied in project-status.yaml CLAUDE.md .claude; do
  if [[ -e "$dest/$denied" ]]; then
    echo "公開してはいけない $denied が書き出しに含まれている" >&2
    exit 1
  fi
done

printf '%s\n' "${files[@]}"
