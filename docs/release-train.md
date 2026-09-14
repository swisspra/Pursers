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
   git config --get user.signingkey || echo "no signing key: use -a"
   git tag -a <release_tag> <verified_commit> -m "Pursers <version>" &&
     test "$(git rev-parse --verify <release_tag>^{commit})" = <verified_commit> &&
     git push origin <release_tag>
   ```

   If a pre-push command fails, delete the unpushed local tag with
   `git tag -d <release_tag>` before retrying. Signing is optional: use `-s`
   only with GPG or `gpg.format=ssh` plus `user.signingkey` configured to a
   project-owned key, and insert `git tag -v <release_tag> &&` before the SHA
   check. The release workflow verifies that the tag exists; it does not
   verify tag signatures.

PyPI versions are immutable. If verification fails after a version has been
published, advance the affected version instead of rebuilding that release.

## Home runtime wheelhouse lock

`tools/home_runtime_wheelhouse.lock` pins every third-party wheel by exact
version and SHA-256 for the supported Python 3.12 platform. Normal wheelhouse
builds consume that committed lock with pip's `--no-deps --require-hashes`;
they do not resolve against the current package index. The resulting
`wheelhouse.json` records the lock path, lock SHA-256, and source-requirements
SHA-256.

Build from the committed lock with a clean checkout and an explicit Python
3.12 interpreter:

```sh
python3 tools/build_home_runtime_wheelhouse.py \
  --python /PATH/TO/python3.12 \
  --output /ABSOLUTE/PATH/home-runtime-wheelhouse
```

Only refresh the lock as a reviewed release-pipeline change. Refreshing is
platform-specific and overwrites the selected lock atomically:

```sh
python3 tools/build_home_runtime_wheelhouse.py \
  --python /PATH/TO/python3.12 \
  --refresh-lock
git diff -- tools/home_runtime_wheelhouse.lock
```

The builder fails before dependency resolution when the lock's Python,
platform, or source-requirements fingerprint is stale. Commit the refreshed
lock together with the dependency change; never hand-edit its pins or hashes.
