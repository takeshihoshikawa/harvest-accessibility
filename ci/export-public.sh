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
# -z で NUL 区切りにする。既定では非 ASCII を含むパスが "..\346.." の形に
# 引用されて出るため、接頭辞の照合に失敗してそのファイルが黙って公開から漏れる。
while IFS= read -r -d '' f; do
  for r in "${rules[@]}"; do
    if [[ "$r" == */ ]]; then
      [[ "$f" == "$r"* ]] && { files+=("$f"); break; }
    else
      [[ "$f" == "$r" ]] && { files+=("$f"); break; }
    fi
  done
done < <(git ls-tree -r -z --name-only "$ref")

if [[ ${#files[@]} -eq 0 ]]; then
  echo "公開対象が1件も無い。許可リストと ref を確認する: $ref" >&2
  exit 1
fi

mkdir -p "$dest"
# :(literal) を付けて、ファイル名に含まれる [ ] * を git にグロブとして
# 解釈させない。解釈されると意図しないファイルまで書き出されうる。
pathspecs=()
for f in "${files[@]}"; do pathspecs+=(":(literal)$f"); done
git archive "$ref" -- "${pathspecs[@]}" | tar -x -C "$dest"

# ミラーは自分に打たれたタグで Actions を走らせて Release を作るので、
# そのワークフローだけは名前を変えて渡す。作業リポ側では .github/ の下に
# 置かない（置くと作業リポでも Release ができてしまう）。
mkdir -p "$dest/.github/workflows"
git show "$ref:ci/mirror-release.yml" > "$dest/.github/workflows/release.yml"

# 保険。許可リストを書き換えたときに気づけるようにする。
# 許可はディレクトリ接頭辞で効くので、harvest_accessibility/CLAUDE.md のように
# 配下へ置かれたものも混入しうる。トップだけでなく全階層を見る。
found=0
while IFS= read -r hit; do
  echo "公開してはいけないファイルが書き出しに含まれている: ${hit#"$dest"/}" >&2
  found=1
done < <(find "$dest" \( -name CLAUDE.md -o -name project-status.yaml -o -name .claude \))
[[ $found -eq 0 ]] || exit 1

printf '%s\n' "${files[@]}"
