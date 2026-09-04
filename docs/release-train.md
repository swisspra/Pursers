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
