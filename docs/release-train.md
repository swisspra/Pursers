# Release train

`tools/release_versions.toml` is the single source for the product, package,
wait-bridge, reproducible-build toolchain, and source-date versions. Package
metadata, runtime constants, dashboard sources, local manuals, release tests,
and the Personal component lock are consumers. Do not edit those pins by hand.

## Cut a train

1. Bump and review the generated diff:

   ```sh
   python3 tools/release_train.py bump \
     --set product=<product_version> \
     --set central=<central_version> \
     --set client=<client_version> \
     --set import=<import_version> \
     --set wait_bridge=<bridge_version>
   ```

   For a train where every alpha counter advances once, use
   `python3 tools/release_train.py bump --next patch-alpha`. Add `--dry-run` to
   print the proposed patch without writing files or rebuilding the component
   lock.

2. Verify generated consumers and the release suites:

   ```sh
   python3 tools/release_train.py check
   python3 -m pytest -q tools/tests tools/wait-bridge/tests packages/personal/tests
   ```

   CI builds all five main wheels and the wait-bridge wheel into one directory
   before tests. `PIP_FIND_LINKS` and `UV_FIND_LINKS` point installers at those
   sibling artifacts, so a new client pin is tested before it exists on PyPI.

3. Tag the exact verified commit:

   ```sh
   git tag -s <release_tag> <verified_commit>
   git push origin <release_tag>
   ```

PyPI versions are immutable. If verification fails after a version has been
published, advance the affected version instead of rebuilding that release.

## GitHub prerelease handoff

The release workflow validates that the tag is canonical PEP 440 and exactly
matches `tools/release_versions.toml`. It also requires the six wheel filenames
to match the manifest versions before it creates `SHA256SUMS.txt`. Alpha, beta,
and release-candidate tags use `--prerelease --latest=false` on both the create
and existing-release paths. Stable tags remain the latest release.

For `v5.0.0b1`, the coordinator and operator must replace
`APPROVED_FULL_40_HEX_SHA` below
with the same exact candidate that passed source review, all test suites,
browser/201-behavior evidence, CI, and CodeQL. The preparation branch is not a
tag candidate by itself.

```sh
set -euo pipefail
TAG=v5.0.0b1
CANDIDATE="APPROVED_FULL_40_HEX_SHA"
test "$(git rev-parse "$CANDIDATE^{commit}")" = "$CANDIDATE"
git tag -s "$TAG" "$CANDIDATE"
git push origin "refs/tags/$TAG"
RUN_ID=""
for attempt in {1..30}; do
  RUN_ID="$(gh run list --repo swisspra/Pursers --workflow release.yml \
    --event push --branch "$TAG" --commit "$CANDIDATE" --limit 1 \
    --json databaseId --jq '.[0].databaseId')"
  test -n "$RUN_ID" && test "$RUN_ID" != null && break
  sleep 2
done
test -n "$RUN_ID" && test "$RUN_ID" != null
gh run watch "$RUN_ID" --repo swisspra/Pursers --exit-status
gh release view "$TAG" --repo swisspra/Pursers \
  --json tagName,isPrerelease,assets,targetCommitish
test "$(gh release view "$TAG" --repo swisspra/Pursers \
  --json isPrerelease --jq '.isPrerelease')" = true
LATEST_STABLE="$(gh api repos/swisspra/Pursers/releases/latest \
  --jq '.tag_name' 2>/dev/null || true)"
test "$LATEST_STABLE" != "$TAG"
gh release download "$TAG" --repo swisspra/Pursers --dir dist-release
(cd dist-release && shasum -a 256 -c SHA256SUMS.txt)
```

Expected artifacts are the six manifest-bound wheels plus `SHA256SUMS.txt`.
This beta train authorizes no PyPI publication or production cutover. If the
tag, release state, cohort, or checksum is wrong, stop without installing or
deploying it. Do not move the tag or replace assets under the same version;
correct the source and advance to a new prerelease version. Because no service
cutover is authorized here, rollback is limited to continuing to use the prior
approved release.
