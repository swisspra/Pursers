# Pursers 5.0.0b1 release-day runbook

This runbook is for the operator publishing the first GitHub beta from the
reviewed rc6 commit. It does not authorize a tag, GitHub Release, upload,
version change, PyPI publish, or `main` push.

The release candidate is exactly:

```text
dc5847395e619359f6ba06e6f8d19fd2a7ec7bd5
```

Read this together with the [release train guide](../release-train.md), the
[release body](v5.0.0b1.md), and the
[release workflow](../../.github/workflows/release.yml).

Run every shell block below with `/bin/sh` (the equivalent script shebang is
`#!/bin/sh`), not by pasting it into an interactive zsh session.

## Rehearsal result and release gates

On 2026-09-14, the non-publishing rehearsal reproduced the six-wheel build,
checksum, and Home runtime wheelhouse procedures exactly. It built all six
wheels from a `git archive` of the exact candidate with Python 3.12 and the
pinned build toolchain, and their generated `SHA256SUMS.txt` matched the
approved set byte for byte. It also verified the separately archived 30-wheel
Home runtime wheelhouse: all 30 wheels passed their recorded checksums, and the
wheelhouse `SHA256SUMS` file hashed to
`a34844ff32687fb8b30b9c03c990db35eec74dd88296dfcb0dd450001bbac4c5`.

| Artifact | Rehearsed SHA-256 | Result |
| --- | --- | --- |
| `pursers-5.0.0b1-py3-none-any.whl` | `af2d2722652ce5f952b50d4a50933edc13e12a10a88977c8344bf76b2faf87cd` | match |
| `pursers_central-0.1.0a30-py3-none-any.whl` | `197fee2894b1c7b0204f844a4c2030d4a9245489dd8fca61693fca6e09a6a636` | match |
| `pursers_client-0.1.0a23-py3-none-any.whl` | `e2314191a354ab2ab0d0020c1ef80cc0049909eaf5219fcdff0c0f24847b677e` | match |
| `pursers_personal-5.0.0b1-py3-none-any.whl` | `2946cde056bfc1a6b38c1e7b07ed8b3d71ba1b77a7fb3b4e69f7f81db1839935` | match |
| `pursers_personal_import-5.0.0a3-py3-none-any.whl` | `34e7d992dffc7b50560706ecdb4c51a5ce67e50edb5c24ec8de2cd48259ed590` | match |
| `pursers_wait_bridge-0.1.0a16-py3-none-any.whl` | `102d6eb35daae393c8fef589c8b3e60b02ab985d01856f64afb99ba3ba94834f` | match |

Two documentation/automation gates govern publication. Do not publish until
both are resolved and reviewed:

1. `docs/releases/v5.0.0b1.md` must name
   `dc5847395e619359f6ba06e6f8d19fd2a7ec7bd5` in its `Built from` provenance.
   The `grep` in step 6 is the hard gate against the prior body, which named
   `28f81308d1cf3d40c4ed38091cc02d9c7d0827aa`.
2. The tag-triggered workflow renders `release-notes.md` from `CHANGELOG.md`.
   It does not use `docs/releases/v5.0.0b1.md`; the two rendered bodies differ.
   Decide and review which body is authoritative before pushing the tag.

The tag-triggered workflow and a manual `gh release create` must never run at
the same time. The normal path is the workflow. The manual commands below are
only a controlled recovery path after the workflow has stopped before release
creation and the operator has confirmed no release job is still running.

## 1. Prepare an exact clean checkout

Use a fresh directory. Do not reuse wheels or a release body from another
checkout.

```sh
set -eu
TAG=v5.0.0b1
VERSION=5.0.0b1
CANDIDATE=dc5847395e619359f6ba06e6f8d19fd2a7ec7bd5
RC_BRANCH=codex/TK-f5c5239432d4-b1-rc2
STAGING=/PATH/TO/RELEASE-STAGING
REPOSITORY=https://github.com/swisspra/Pursers.git

mkdir -p "$STAGING"
git clone --no-checkout "$REPOSITORY" "$STAGING/repository"
git -C "$STAGING/repository" fetch origin \
  "refs/heads/${RC_BRANCH}:refs/remotes/origin/${RC_BRANCH}"
test "$(git -C "$STAGING/repository" rev-parse --verify \
  "refs/remotes/origin/${RC_BRANCH}^{commit}")" = "$CANDIDATE"
git -C "$STAGING/repository" checkout --detach "$CANDIDATE"
test "$(git -C "$STAGING/repository" rev-parse --verify HEAD^{commit})" = "$CANDIDATE"
test -z "$(git -C "$STAGING/repository" status --porcelain)"
```

Expected final output:

```text
HEAD is now at dc58473 chore(release): freeze rc6 integration manifest
```

The two `test` commands are silent and exit zero. If either fails, stop.

## 2. Build the six-wheel cohort

Use Python 3.12 and exactly the versions pinned in
`tools/release_versions.toml`:

```sh
cd "$STAGING/repository"
python3.12 -m venv "$STAGING/build-venv"
"$STAGING/build-venv/bin/python" -m pip install \
  build==1.3.0 setuptools==80.9.0 wheel==0.45.1 \
  packaging==25.0 pyproject-hooks==1.2.0

"$STAGING/build-venv/bin/python" - <<'PY'
import importlib.metadata as metadata

expected = {
    "build": "1.3.0",
    "setuptools": "80.9.0",
    "wheel": "0.45.1",
    "packaging": "25.0",
    "pyproject-hooks": "1.2.0",
}
actual = {name: metadata.version(name) for name in expected}
if actual != expected:
    raise SystemExit(f"build toolchain mismatch: {actual!r} != {expected!r}")
print(f"build_toolchain={actual}")
PY

test ! -e dist
mkdir dist
export SOURCE_DATE_EPOCH=315532800
export PYTHONHASHSEED=0
for project in central client import personal; do
  "$STAGING/build-venv/bin/python" -m build --wheel --no-isolation \
    --outdir dist "packages/$project"
done
"$STAGING/build-venv/bin/python" -m pip wheel \
  --no-deps --no-build-isolation --wheel-dir dist \
  packages/pursers tools/wait-bridge

"$STAGING/build-venv/bin/python" - <<'PY'
from pathlib import Path
from tools.release_versions import expected_wheel_filenames

actual = {wheel.name for wheel in Path("dist").glob("*.whl")}
expected = set(expected_wheel_filenames())
if actual != expected:
    raise SystemExit(
        f"release wheel cohort mismatch: actual={sorted(actual)!r}; "
        f"expected={sorted(expected)!r}"
    )
print("release_wheels=" + ",".join(sorted(actual)))
PY
```

Expected cohort output:

```text
release_wheels=pursers-5.0.0b1-py3-none-any.whl,pursers_central-0.1.0a30-py3-none-any.whl,pursers_client-0.1.0a23-py3-none-any.whl,pursers_personal-5.0.0b1-py3-none-any.whl,pursers_personal_import-5.0.0a3-py3-none-any.whl,pursers_wait_bridge-0.1.0a16-py3-none-any.whl
```

## 3. Rehearse manifest and GitHub Release flags

Run the release tools with the build environment from step 2. It contains the
pinned `packaging==25.0`; an unprepared system Python is not sufficient.

```sh
cd "$STAGING/repository"
"$STAGING/build-venv/bin/python" tools/release_train.py check
"$STAGING/build-venv/bin/python" tools/release_publish.py "$TAG" create
"$STAGING/build-venv/bin/python" tools/release_publish.py "$TAG" edit
```

Expected output:

```text
release versions OK: product=5.0.0b1
--prerelease
--latest=false
--prerelease
--latest=false
```

Before the tag exists, this check is expected to fail because it deliberately
requires `refs/tags/v5.0.0b1`:

```sh
"$STAGING/build-venv/bin/python" tools/release_publish.py \
  "$TAG" verify-checkout
```

Do not create a temporary tag just to make the rehearsal pass. Run the same
command again after the operator creates the real signed tag in step 7; its
only expected output then is the full candidate SHA.

## 4. Generate and verify `SHA256SUMS.txt`

Generate the file in the same filename order as the release workflow:

```sh
cd "$STAGING/repository"
if command -v sha256sum >/dev/null 2>&1; then
  (cd dist && sha256sum -- ./*.whl > SHA256SUMS.txt)
else
  (cd dist && shasum -a 256 ./*.whl > SHA256SUMS.txt)
fi

cat > "$STAGING/approved-SHA256SUMS.txt" <<'EOF'
af2d2722652ce5f952b50d4a50933edc13e12a10a88977c8344bf76b2faf87cd  ./pursers-5.0.0b1-py3-none-any.whl
197fee2894b1c7b0204f844a4c2030d4a9245489dd8fca61693fca6e09a6a636  ./pursers_central-0.1.0a30-py3-none-any.whl
e2314191a354ab2ab0d0020c1ef80cc0049909eaf5219fcdff0c0f24847b677e  ./pursers_client-0.1.0a23-py3-none-any.whl
2946cde056bfc1a6b38c1e7b07ed8b3d71ba1b77a7fb3b4e69f7f81db1839935  ./pursers_personal-5.0.0b1-py3-none-any.whl
34e7d992dffc7b50560706ecdb4c51a5ce67e50edb5c24ec8de2cd48259ed590  ./pursers_personal_import-5.0.0a3-py3-none-any.whl
102d6eb35daae393c8fef589c8b3e60b02ab985d01856f64afb99ba3ba94834f  ./pursers_wait_bridge-0.1.0a16-py3-none-any.whl
EOF

diff -u "$STAGING/approved-SHA256SUMS.txt" dist/SHA256SUMS.txt
(cd dist && shasum -a 256 -c SHA256SUMS.txt)
```

`diff` must be silent and exit zero. The checksum command must report `OK` for
all six wheels. These exact bytes are the `SHA256SUMS.txt` the release carries.
Any difference is a hard stop; do not upload the new bytes under this version.

## 5. Verify the archived Home runtime wheelhouse

The b1 Home runtime dependency resolver was not locked when rc6 was approved.
A fresh resolution has already drifted (`uvicorn` 0.52.4 to 0.53.0), so it is
not a reproducible replacement for the approved wheelhouse. Use the archived
30-wheel directory only; do not run `build_home_runtime_wheelhouse.py` to
create b1 upload bytes.

The archive path is operator-owned. Use a public-safe path in logs and tickets:

```sh
APPROVED_HOME_WHEELHOUSE=/PATH/TO/ARCHIVED/wheelhouse-approved-a34844ff
HOME_RUNTIME_UPLOAD=/PATH/TO/EMPTY/HOME-RUNTIME-UPLOAD
HOME_SHA256SUMS_SHA256=a34844ff32687fb8b30b9c03c990db35eec74dd88296dfcb0dd450001bbac4c5

test "$(find "$APPROVED_HOME_WHEELHOUSE" -maxdepth 1 -type f \
  -name '*.whl' | wc -l | tr -d ' ')" = 30
test "$(shasum -a 256 "$APPROVED_HOME_WHEELHOUSE/SHA256SUMS" | \
  awk '{print $1}')" = "$HOME_SHA256SUMS_SHA256"
(cd "$APPROVED_HOME_WHEELHOUSE" && shasum -a 256 -c SHA256SUMS)

test ! -e "$HOME_RUNTIME_UPLOAD"
mkdir -p "$HOME_RUNTIME_UPLOAD"
cp -p "$APPROVED_HOME_WHEELHOUSE"/*.whl \
  "$APPROVED_HOME_WHEELHOUSE/SHA256SUMS" \
  "$APPROVED_HOME_WHEELHOUSE/wheelhouse.json" \
  "$HOME_RUNTIME_UPLOAD/"
test "$(find "$HOME_RUNTIME_UPLOAD" -maxdepth 1 -type f \
  -name '*.whl' | wc -l | tr -d ' ')" = 30
test "$(shasum -a 256 "$HOME_RUNTIME_UPLOAD/SHA256SUMS" | \
  awk '{print $1}')" = "$HOME_SHA256SUMS_SHA256"
(cd "$HOME_RUNTIME_UPLOAD" && shasum -a 256 -c SHA256SUMS)
```

Expected output is 30 `OK` lines for the archive and the same 30 `OK` lines
for the upload staging directory; the count and top-level hash assertions are
silent. This is a separate Home runtime deployment input, not part of the
seven-asset GitHub Release cohort. If a later transport is required, upload
these verified bytes from `HOME_RUNTIME_UPLOAD` without rebuilding or
renaming them, then repeat both hash checks at the destination.

The committed dependency-lock repair is tracked as beta.2 fix
`TK-143f427367aa` (release tooling: wheelhouse builds install from a committed
hash lock). That future fix does not authorize regenerating the frozen b1 set.

## 6. Render and approve the release body

The release body is a reviewed docs artifact, not a file from the rc6 commit.
Use a separate clean checkout containing the reviewed correction of
`docs/releases/v5.0.0b1.md`, and record its exact commit as `DOCS_COMMIT`:

```sh
DOCS_REPOSITORY=/PATH/TO/REVIEWED-DOCS-CHECKOUT
DOCS_COMMIT=$(git -C "$DOCS_REPOSITORY" rev-parse --verify HEAD^{commit})
test -z "$(git -C "$DOCS_REPOSITORY" status --porcelain)"
grep -F "$CANDIDATE" "$DOCS_REPOSITORY/docs/releases/v5.0.0b1.md"
cp "$DOCS_REPOSITORY/docs/releases/v5.0.0b1.md" "$STAGING/release-body.md"
test -s "$STAGING/release-body.md"
printf 'release_body_commit=%s\n' "$DOCS_COMMIT"
```

Expected output includes the full candidate SHA and a reviewed docs commit.
The `grep` must pass on the reviewed correction and remains an intentional
pre-publish gate. Do not substitute the shorter CHANGELOG body without review.

## 7. Operator-only tag creation

Only the release operator performs these commands, after all gates above are
green and the exact candidate has passed the required CI and review:

```sh
cd "$STAGING/repository"
git tag -s "$TAG" "$CANDIDATE" -m "Pursers $VERSION"
test "$(git rev-parse --verify "$TAG^{commit}")" = "$CANDIDATE"
python3 tools/release_publish.py "$TAG" verify-checkout
git push origin "refs/tags/$TAG"
```

Expected `verify-checkout` output:

```text
dc5847395e619359f6ba06e6f8d19fd2a7ec7bd5
```

Pushing the tag starts the repository's `release` workflow. Watch that run to
completion and do not run the manual create path while it is active:

```sh
RUN_ID=$(gh run list --workflow release --event push --limit 20 \
  --json databaseId,headBranch \
  --jq ".[] | select(.headBranch == \"$TAG\") | .databaseId" | head -n 1)
test -n "$RUN_ID"
gh run watch "$RUN_ID" --exit-status
```

## 8. Controlled manual recovery only

Use this section only if the tag-triggered workflow failed before creating a
release, no release job is still running, the local seven assets passed the
approved hash gate, and the coordinator explicitly selected the reviewed docs
body. The flags below are mandatory for a beta:

```sh
cd "$STAGING/repository"
test "$(git rev-parse --verify "$TAG^{commit}")" = "$CANDIDATE"
test -z "$(gh run list --workflow release --status in_progress --limit 20 \
  --json headBranch --jq ".[] | select(.headBranch == \"$TAG\") | .headBranch")"

gh release create "$TAG" \
  dist/*.whl dist/SHA256SUMS.txt \
  --verify-tag \
  --title "Pursers $VERSION" \
  --notes-file "$STAGING/release-body.md" \
  --prerelease \
  --latest=false
```

If the matching prerelease already exists but is missing an asset, first
download and compare every existing same-name asset. Never use `--clobber`:

```sh
mkdir -p "$STAGING/existing-release-assets"
gh release download "$TAG" --dir "$STAGING/existing-release-assets"
for local_asset in dist/*.whl dist/SHA256SUMS.txt; do
  name=$(basename "$local_asset")
  existing="$STAGING/existing-release-assets/$name"
  if test -e "$existing"; then
    python3 tools/release_publish.py "$TAG" verify-asset \
      --local "$local_asset" --existing "$existing"
  else
    gh release upload "$TAG" "$local_asset"
  fi
done
gh release edit "$TAG" \
  --title "Pursers $VERSION" \
  --notes-file "$STAGING/release-body.md" \
  --prerelease \
  --latest=false
```

Each `verify-asset` invocation prints that asset's approved SHA-256. A mismatch
must stop the recovery; do not replace the existing asset.

## 9. Post-publish verification

Verify the tag, release state, exact seven-asset set, checksums, install path,
and stable-latest boundary from a fresh download directory:

```sh
cd "$STAGING/repository"
git fetch origin "refs/tags/$TAG:refs/tags/$TAG"
test "$(git rev-parse --verify "$TAG^{commit}")" = "$CANDIDATE"

gh release view "$TAG" \
  --json tagName,isDraft,isPrerelease,targetCommitish,assets \
  --jq '{tagName,isDraft,isPrerelease,targetCommitish,assets:[.assets[].name]}'

DOWNLOADS="$STAGING/post-publish-download"
mkdir "$DOWNLOADS"
gh release download "$TAG" --dir "$DOWNLOADS"
test "$(find "$DOWNLOADS" -maxdepth 1 -type f | wc -l | tr -d ' ')" = 7
diff -u "$STAGING/approved-SHA256SUMS.txt" "$DOWNLOADS/SHA256SUMS.txt"
(cd "$DOWNLOADS" && shasum -a 256 -c SHA256SUMS.txt)

LATEST_STABLE=$(gh api repos/swisspra/Pursers/releases/latest --jq .tag_name)
test "$LATEST_STABLE" != "$TAG"

python3.12 -m venv "$STAGING/install-venv"
"$STAGING/install-venv/bin/python" -m pip install \
  --no-index --no-deps "$DOWNLOADS"/*.whl
# The six release wheels are now pinned locally. Deliberately allow the package
# index only for their third-party dependencies; the seven-asset release does
# not contain a complete dependency wheelhouse.
"$STAGING/install-venv/bin/python" -m pip install \
  "pursers==$VERSION" "pursers-wait-bridge==0.1.0a16"
"$STAGING/install-venv/bin/python" -m pip check
```

The release JSON must show `isDraft: false`, `isPrerelease: true`, the expected
tag, and exactly the six wheel names plus `SHA256SUMS.txt`. All checksum lines
must say `OK`; `pip check` must report `No broken requirements found.` The
stable-latest assertion must be silent and exit zero. The first install must
name all six downloaded local wheel paths; the second may contact the configured
package index for third-party dependencies only. If fully offline installation
is required, use a separately reviewed complete dependency wheelhouse rather
than claiming the seven GitHub assets are sufficient.

## 10. Stop and rollback rules

- Before tag push: stop, preserve logs, discard the staging artifacts, and
  rebuild from a fresh exact-candidate checkout. Nothing external needs
  rollback.
- After tag push but before release publication: do not move or recreate the
  signed tag. Stop the release job, record the mismatch, fix the source, and
  advance to a new prerelease candidate and tag.
- After publication: do not replace assets, reuse a version, move the tag, or
  delete evidence. Mark the release unavailable only under an explicit
  operator/coordinator incident decision, keep the previous approved release
  as the recovery target, correct the source, advance the prerelease version,
  and publish a new signed tag.
- At every stage, an unexpected asset, same-name asset hash mismatch, wrong
  candidate, non-prerelease release, beta marked latest, empty body, or body
  provenance mismatch is a hard stop.

## Rehearsal observations

- `"$STAGING/build-venv/bin/python" tools/release_train.py check` passed at rc6 with
  `release versions OK: product=5.0.0b1`.
- `release_publish.py` emitted `--prerelease` and `--latest=false` for both
  create and edit modes.
- All six reproducible wheel hashes matched the approved set, and the detached
  rc6 checkout remained clean.
- The archived Home runtime wheelhouse contains 30 wheels, all 30 checksum
  lines pass, and its `SHA256SUMS` hash is exactly
  `a34844ff32687fb8b30b9c03c990db35eec74dd88296dfcb0dd450001bbac4c5`.
  A fresh b1 wheelhouse resolution must not replace it; `TK-143f427367aa`
  carries the committed-lock fix for beta.2.
- A genuinely fresh Python 3.12 venv installed all six local release wheels
  with `--no-index --no-deps`, then resolved third-party dependencies through
  the package index and finished with `No broken requirements found.` The
  earlier six-wheel-only `--no-index` command was invalid because the release
  cohort is not a complete dependency wheelhouse.
- `verify-checkout` correctly failed before the tag existed. It becomes an
  effective exact-SHA gate only after the signed tag is created.
- The curated release body is absent from rc6. Its reviewed docs correction
  must name the exact candidate, and it differs from the CHANGELOG-derived body
  used by the workflow. Body provenance remains a release-day gate, not a
  reason to bypass review.
- A manual `gh release create` can race the tag-triggered workflow. Use the
  workflow normally; use the manual path only after a stopped pre-create run
  and an explicit recovery decision.
