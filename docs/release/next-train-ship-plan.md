# Next train ship plan

Status: preparation only. The final release-train ticket owns every version
write, merge, tag, publish, deployment, cutover, and rollback action. This plan
must fail closed when an immutable input or equality check is missing.

## 1. Immutable inputs and provisional version map

The last released tag is `v5.0.0a25` at
`c2ebac5de803a0f7a00468ec4d3cdf06e4719096`. The preparation baseline is frozen
main `b06ce6627edb62fc588eee541fa568445b709054`.

The final integrator must record these values before changing versions:

- `FINAL_ASSEMBLY_SHA`: independently approved successor from
  `TK-a3f0627d27db`, including all-nine browser observations, full inventory,
  rotation/reconnect, and final guide alignment.
- `QUICKSTART_SHA=909e897e960cc68139a043e2494aa4f6600b47ce`, approved through
  `TK-f50ed337bfd3`.
- `SECURITY_RECEIPT_SHA=d2ea1237f50f06193eb03ad32fead93ab197b349`, approved
  through `TK-caecfe4f5ad1`; its final receipt is
  `docs/security/next-train-codeql-receipt.md`.
- `O1_TOOLKIT_SHA=10f29f122dbc6975f5f272b7315d9fd58eebc3ab`, approved through
  `TK-eeb3ac4fba84`.
- `INTEGRATED_SOURCE_SHA`: one reviewed source commit containing exactly those
  approved inputs, before release-only version edits.

Before any release command, require the blob at
`INTEGRATED_SOURCE_SHA:docs/security/next-train-codeql-receipt.md` to equal the
same path at `SECURITY_RECEIPT_SHA`; a missing path or byte difference blocks
the train. This adopted receipt does not replace fresh exact-SHA CodeQL evidence:
generate new candidate evidence after integration and version edits, then new
exact-main evidence after the reviewed candidate reaches `main`.

Observed changes between `v5.0.0a25` and the current assembly candidate affect
Central, Client, Personal/Home, and wait-bridge. `packages/import` is unchanged.
The release helper requires product, `pursers`, and `personal` to match. Subject
to a final changed-path recheck, the next map is:

| Key | Released | Next | Reason |
| --- | --- | --- | --- |
| `product` | `5.0.0a25` | `5.0.0a26` | train identity changes |
| `pursers` | `5.0.0a25` | `5.0.0a26` | coupled to product |
| `personal` | `5.0.0a25` | `5.0.0a26` | Home resources changed; coupled to product |
| `central` | `0.1.0a29` | `0.1.0a30` | Central runtime changed |
| `client` | `0.1.0a22` | `0.1.0a23` | Client and registry behavior changed |
| `wait_bridge` | `0.1.0a15` | `0.1.0a16` | lifecycle and recovery changed |
| `import` | `5.0.0a3` | `5.0.0a3` | no package change; do not bump |

Before using the map, compare `v5.0.0a25...INTEGRATED_SOURCE_SHA`. If a package
set differs from the table, stop, explain the path-to-package mapping, and have
the final release ticket approve a corrected map. Never use `--next
patch-alpha`, because it would bump unchanged `import`.

## 2. Integrate, bump, and bind the final candidate

Adopt each approved commit with preserved provenance. Confirm every selected
blob and review record before resolving conflicts; do not reconstruct an
approved change by hand. The combined source receives independent review at
`INTEGRATED_SOURCE_SHA`.

Run the release helper first in dry-run mode, review its complete patch, then
run the same explicit map without `--dry-run` only in the activated final
release-train ticket:

```sh
python3 tools/release_train.py bump --dry-run \
  --set product=5.0.0a26 \
  --set central=0.1.0a30 \
  --set client=0.1.0a23 \
  --set wait_bridge=0.1.0a16

python3 tools/release_train.py bump \
  --set product=5.0.0a26 \
  --set central=0.1.0a30 \
  --set client=0.1.0a23 \
  --set wait_bridge=0.1.0a16

python3 tools/release_train.py check
python3 tools/release_notes.py 5.0.0a26
```

The helper, not manual edits, owns `tools/release_versions.toml`, `CHANGELOG.md`,
all package metadata and runtime version consumers, documentation consumers,
`tools/regenerate_component_lock.py`, and
`packages/personal/src/pursers_personal/resources/component-lock.json`. Review
the dry-run affected paths against `tools/release_train.py::VERSION_FILES`.
`packages/import/pyproject.toml` must remain at `5.0.0a3`.

Commit those generated changes once. Call that exact commit
`RELEASE_CANDIDATE_SHA`. From this point, every source archive, wheel, Home
package, lock, test result, browser observation, and security result must name
that SHA. A change to any byte creates a new candidate and invalidates all
candidate-bound evidence.

## 3. Fresh candidate gates

Use a new checkout and new Python 3.12 environment. Install the toolchain
versions from `tools/release_versions.toml`; build local wheels before tests so
new inter-package pins never resolve from PyPI.

```sh
python3 -m venv /PATH/TO/release-gate-venv
/PATH/TO/release-gate-venv/bin/python -m pip install \
  build==1.3.0 setuptools==80.9.0 wheel==0.45.1 \
  packaging==25.0 pyproject-hooks==1.2.0 'pytest>=8' uv

export SOURCE_DATE_EPOCH=315532800
mkdir -p /PATH/TO/candidate-wheels
for project in pursers central client personal import; do
  /PATH/TO/release-gate-venv/bin/python -m build --wheel --no-isolation \
    --outdir /PATH/TO/candidate-wheels "packages/$project"
done
/PATH/TO/release-gate-venv/bin/python -m build --wheel --no-isolation \
  --outdir /PATH/TO/candidate-wheels tools/wait-bridge

export PIP_FIND_LINKS=/PATH/TO/candidate-wheels
export UV_FIND_LINKS=/PATH/TO/candidate-wheels
/PATH/TO/release-gate-venv/bin/python -m pip install \
  /PATH/TO/candidate-wheels/pursers_central-*.whl \
  /PATH/TO/candidate-wheels/pursers_client-*.whl
/PATH/TO/release-gate-venv/bin/python -m pip install --no-deps \
  --editable 'packages/personal[test]' --editable packages/import
/PATH/TO/release-gate-venv/bin/python -m pip check

/PATH/TO/release-gate-venv/bin/python tools/ci_manifest.py check
/PATH/TO/release-gate-venv/bin/python tools/ci_manifest.py collect \
    --output /PATH/TO/candidate-collection.json
/PATH/TO/release-gate-venv/bin/python tools/ci_manifest.py run
/PATH/TO/release-gate-venv/bin/python tools/ci_manifest.py verify \
  --input /PATH/TO/candidate-collection.json
```

All 11 manifest suites must collect nonzero tests and pass: central, client,
import, personal, wait-bridge, Fleet Dashboard, coordinator, worker runtime,
seat-kit, AionUI extension, and release tools. Also require:

```sh
python3 tools/release_train.py check
python3 tools/ci_manifest.py check
python3 tools/verify_publish_wheels.py --wheel-dir /PATH/TO/candidate-wheels
python3 tools/leak_scan.py
python3 -m py_compile tools/release_train.py tools/release_versions.py \
  tools/regenerate_component_lock.py tools/o1_cutover.py
(cd tools/dashboard-ui && npm ci && npm run typecheck && npm run build && npm audit --omit=optional)
node --test tools/aionui-extension/tests/*.test.cjs
git diff --check v5.0.0a25..RELEASE_CANDIDATE_SHA

: "${RELEASE_CANDIDATE_SHA:?set the exact release candidate SHA}"
: "${RELEASE_CANDIDATE_REF:?set refs/pull/NUMBER/head for the reviewed candidate}"
case "$RELEASE_CANDIDATE_REF" in
  refs/pull/*/head) ;;
  *) echo "RELEASE_CANDIDATE_REF must be refs/pull/NUMBER/head" >&2; exit 1 ;;
esac
CANDIDATE_CODEQL_ANALYSES="$(
  gh api --method GET --paginate \
    repos/swisspra/Pursers/code-scanning/analyses \
    -f ref="$RELEASE_CANDIDATE_REF" -f per_page=100 | jq -cs 'add'
)"
jq -e --arg sha "$RELEASE_CANDIDATE_SHA" \
  --arg ref "$RELEASE_CANDIDATE_REF" '
  ([.[] | select(.commit_sha == $sha and .ref == $ref)]
    | sort_by(.category, .created_at)
    | group_by(.category)
    | map(last)) as $exact
  | (($exact | length) == 3)
    and (($exact | map(.category) | sort) ==
      ["/language:actions", "/language:javascript-typescript", "/language:python"])
    and all($exact[];
      .tool.name == "CodeQL" and
      .rules_count > 0 and
      .results_count == 0 and
      .error == "" and
      .warning == "")
' <<<"$CANDIDATE_CODEQL_ANALYSES"
for selector in "$RELEASE_CANDIDATE_REF" "$RELEASE_CANDIDATE_SHA"; do
  CANDIDATE_CODEQL_ALERTS="$(
    gh api --method GET --paginate \
      repos/swisspra/Pursers/code-scanning/alerts \
      -f state=open -f ref="$selector" -f per_page=100 | jq -cs 'add'
  )"
  jq -e 'length == 0' <<<"$CANDIDATE_CODEQL_ALERTS"
done
```

Build all six wheels and the AionUI package twice in separate clean directories
with the same toolchain and `SOURCE_DATE_EPOCH`. The AionUI gate is executable:

```sh
export RELEASE_CANDIDATE_SHA
AION_ARCHIVE="$(python3 -c 'import json; print("pursers-aionui-" + json.load(open("tools/aionui-extension/aion-extension.json"))["version"] + ".zip")')"
AION_BUILD_A="$(mktemp -d)"
AION_BUILD_B="$(mktemp -d)"
python3 tools/aionui-extension/build.py --output "$AION_BUILD_A/$AION_ARCHIVE"
python3 tools/aionui-extension/build.py --output "$AION_BUILD_B/$AION_ARCHIVE"
python3 - "$AION_BUILD_A/$AION_ARCHIVE" "$AION_BUILD_B/$AION_ARCHIVE" \
  "$RELEASE_CANDIDATE_SHA" /PATH/TO/evidence/aionui-receipt.json <<'PY'
import hashlib
import json
import sys
import zipfile
from pathlib import Path

def sha256(data):
    return hashlib.sha256(data).hexdigest()

def receipt(raw):
    path = Path(raw)
    payload = path.read_bytes()
    with zipfile.ZipFile(path) as archive:
        members = []
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            data = archive.read(info.filename)
            members.append({
                "name": info.filename,
                "size": info.file_size,
                "compressed_size": info.compress_size,
                "sha256": sha256(data),
            })
        candidate = json.loads(archive.read("webui/candidate.json"))
    return {
        "filename": path.name,
        "size": len(payload),
        "sha256": sha256(payload),
        "candidate_commit": candidate["candidate_commit"],
        "members": members,
    }

left = receipt(sys.argv[1])
right = receipt(sys.argv[2])
expected_sha = sys.argv[3]
if left != right:
    raise SystemExit("AionUI deterministic receipt mismatch")
if left["candidate_commit"] != expected_sha:
    raise SystemExit("AionUI candidate_commit mismatch")
Path(sys.argv[4]).write_text(json.dumps(left, indent=2, sort_keys=True) + "\n")
print(json.dumps(left, sort_keys=True))
PY
```

The equality covers archive filename, archive size and SHA-256, plus every
member name, uncompressed size, compressed size, and SHA-256. Verify installed
wheel imports in a second empty venv with `PYTHONPATH` unset, including
`pursers_central.central`, `pursers_client`, `pursers_personal`, and
`pursers_wait_server`. Execute the installed `pursers-wait-bridge --help` and
the exact clean-install lifecycle probe approved by the assembly reviewer.

Hash `/PATH/TO/evidence/aionui-receipt.json`. The independent assembly verifier
must record `RELEASE_CANDIDATE_SHA`, archive SHA-256, and receipt SHA-256 in the
browser evidence before installing that exact archive and repeating
all nine authenticated browser observations, full capability inventory,
explicit rotation/reconnect, recovery states, and line-by-line alignment with
Quickstart `909e897e960cc68139a043e2494aa4f6600b47ce`. Earlier observations from a
pre-bump SHA do not bind this candidate. Acceptance requires equality among
the build receipt, installed archive, browser record, and candidate commit.

Require exact-candidate CI and CodeQL checks. Preserve the approved Security
receipt, rerun dependency checks, and require zero unresolved candidate alerts.
The disclosed unsupported agentic-review service is not a CodeQL pass. Do not
dismiss main-only test-fixture alerts without separately authorized maintainer
dispositions.

## 4. Main, tag, GitHub Release, and PyPI

Integrate without changing candidate bytes. `origin/main`,
`RELEASE_CANDIDATE_SHA`, and the future tag target must be equal. If main gains a
merge commit or any file changes, create a new candidate and repeat section 3,
including browser acceptance.

Require the push-triggered exact-main CI and CodeQL runs to report
`headSha=RELEASE_CANDIDATE_SHA`. Only then may the operator create the signed
tag and verify it locally and remotely:

```sh
CI_LIST="$(gh run list --workflow ci.yml --branch main --event push \
  --commit "$RELEASE_CANDIDATE_SHA" \
  --json databaseId,event,headBranch,headSha,status,conclusion)"
CI_RUN_ID="$(jq -er --arg sha "$RELEASE_CANDIDATE_SHA" \
  '[.[] | select(.event == "push" and .headBranch == "main" and .headSha == $sha)] | if length == 1 then .[0].databaseId else error("expected one exact-main push CI run") end' \
  <<<"$CI_LIST")"
gh run watch "$CI_RUN_ID" --exit-status
CI_JSON="$(gh run view "$CI_RUN_ID" \
  --json event,headBranch,headSha,status,conclusion,jobs)"
jq -e --arg sha "$RELEASE_CANDIDATE_SHA" '
  .event == "push" and
  .headBranch == "main" and
  .headSha == $sha and
  .status == "completed" and
  .conclusion == "success" and
  ([.jobs[] | select(.name == "python-tests" and .conclusion == "success")] | length == 1) and
  ([.jobs[] | select(.name == "dashboard-ui-typecheck" and .conclusion == "success")] | length == 1)
' <<<"$CI_JSON"

git fetch origin main
RELEASE_MAIN_SHA="$(git rev-parse refs/remotes/origin/main)"
test "$RELEASE_MAIN_SHA" = "$RELEASE_CANDIDATE_SHA"
RELEASE_MAIN_REF=refs/heads/main
MAIN_CODEQL_ANALYSES="$(
  gh api --method GET --paginate \
    repos/swisspra/Pursers/code-scanning/analyses \
    -f ref="$RELEASE_MAIN_REF" -f per_page=100 | jq -cs 'add'
)"
jq -e --arg sha "$RELEASE_MAIN_SHA" --arg ref "$RELEASE_MAIN_REF" '
  ([.[] | select(.commit_sha == $sha and .ref == $ref)]
    | sort_by(.category, .created_at)
    | group_by(.category)
    | map(last)) as $exact
  | (($exact | length) == 3)
    and (($exact | map(.category) | sort) ==
      ["/language:actions", "/language:javascript-typescript", "/language:python"])
    and all($exact[];
      .tool.name == "CodeQL" and
      .rules_count > 0 and
      .results_count == 0 and
      .error == "" and
      .warning == "")
' <<<"$MAIN_CODEQL_ANALYSES"
MAIN_CODEQL_ALERTS="$(
  gh api --method GET --paginate \
    repos/swisspra/Pursers/code-scanning/alerts \
    -f state=open -f ref="$RELEASE_MAIN_REF" -f per_page=100 | jq -cs 'add'
)"
jq -e 'length == 0' <<<"$MAIN_CODEQL_ALERTS"

git tag -s v5.0.0a26 "$RELEASE_CANDIDATE_SHA"
git push origin v5.0.0a26
git rev-parse v5.0.0a26^{}
git ls-remote origin refs/tags/v5.0.0a26 refs/tags/v5.0.0a26^{}
```

The tag-triggered `.github/workflows/release.yml` must build six wheels, create
`SHA256SUMS.txt`, and publish a GitHub Release from the tag. Download to a new
directory and verify every digest and filename:

```sh
gh release download v5.0.0a26 --repo swisspra/Pursers \
  --dir /PATH/TO/release-a26
(cd /PATH/TO/release-a26 && shasum -a 256 -c SHA256SUMS.txt)
python3 tools/verify_publish_wheels.py --wheel-dir /PATH/TO/release-a26
```

The downloaded wheel hashes must equal the candidate build hashes. Run PyPI
Trusted Publishing from the tag ref, not a moving default branch, and verify
the dispatch run and both publish jobs bind the signed tag SHA:

```sh
gh workflow run publish-pypi.yml --ref v5.0.0a26
PUBLISH_LIST="$(gh run list --workflow publish-pypi.yml \
  --branch v5.0.0a26 --event workflow_dispatch \
  --commit "$RELEASE_CANDIDATE_SHA" \
  --json databaseId,event,headBranch,headSha,status,conclusion)"
PUBLISH_RUN_ID="$(jq -er --arg sha "$RELEASE_CANDIDATE_SHA" \
  '[.[] | select(.event == "workflow_dispatch" and .headBranch == "v5.0.0a26" and .headSha == $sha)] | if length == 1 then .[0].databaseId else error("expected one exact-tag PyPI run") end' \
  <<<"$PUBLISH_LIST")"
gh run watch "$PUBLISH_RUN_ID" --exit-status
PUBLISH_JSON="$(gh run view "$PUBLISH_RUN_ID" \
  --json event,headBranch,headSha,status,conclusion,jobs)"
jq -e --arg sha "$RELEASE_CANDIDATE_SHA" '
  .event == "workflow_dispatch" and
  .headBranch == "v5.0.0a26" and
  .headSha == $sha and
  .status == "completed" and
  .conclusion == "success" and
  ([.jobs[] | select(.name == "publish-main" and .conclusion == "success")] | length == 1) and
  ([.jobs[] | select(.name == "publish-wait-bridge" and .conclusion == "success")] | length == 1)
' <<<"$PUBLISH_JSON"
```

After both publish jobs pass, query PyPI for the exact six expected versions,
download them without dependencies into a new directory, and compare hashes
with the tag assets. PyPI versions are immutable: on mismatch or partial
publication, stop and advance only affected versions in a new reviewed release;
never overwrite an existing file.

## 5. Deployment, cutover, and rollback

Keep the currently live release, launcher, data snapshot, JWKS, credentials,
doors, seat folders, coordinator/Fleet configuration, and wheel hashes as one
rollback unit. Do not clean them up during this train.

Use the approved O1 toolkit bytes from
`10f29f122dbc6975f5f272b7315d9fd58eebc3ab`. Populate its operator-private
configuration with the verified tag assets and hashes. Preparation remains
additive:

```sh
python tools/o1_cutover.py --config /PATH/TO/staging/o1-cutover.json prepare
python tools/o1_cutover.py --config /PATH/TO/staging/o1-cutover.json backup
python tools/o1_cutover.py --config /PATH/TO/staging/o1-cutover.json verify-artifacts
python tools/o1_cutover.py --config /PATH/TO/staging/o1-cutover.json dry-run \
  --evidence-out /PATH/TO/staging/cutover-evidence/final.json
```

The operator must freeze dispatch and Teams, drain all active boards, capture
fresh board/membership/seat snapshots, verify distinct canonical live targets,
and review the generated private command sheet. Activation is the
token-invalidating boundary and needs a fresh, successful, zero-mutation
evidence record whose plan hash matches:

```sh
python tools/o1_cutover.py --config /PATH/TO/staging/o1-cutover.json activate \
  --operator-confirmed \
  --evidence /PATH/TO/staging/cutover-evidence/final.json
```

Stop coordinator and Fleet Dashboard first, then Teams, and old Central last.
Start new Central first; validate its expected version, resource, board counts,
storage, and memberships before coordinator, Fleet, or seats. Start one worker
and one independent reviewer canary before the remaining standalone seats.

Rollback immediately on health/version/hash mismatch, missing membership,
issuer/audience/JWKS incoherence, split identity, registry loss, push failure,
dispatch persistence failure, unexpected writer, or data-count drift:

```sh
python tools/o1_cutover.py --config /PATH/TO/staging/o1-cutover.json rollback \
  --operator-confirmed
```

Restore the rollback unit atomically and in reverse journal order. Start old
Central first, then coordinator, Fleet, canary worker, canary reviewer, and the
remaining seats. Resume dispatch only after the old-version health, membership,
registry, and push checks pass.

## 6. Post-cutover acceptance

Before declaring success, require all of the following against the activated
release:

1. Fleet Dashboard returns healthy responses, including the approved `503`
   outage/recovery behavior, without exposing credentials or private paths.
2. Every standalone seat reports its exact identity, role, tier, model/profile
   contract, and independent worker/reviewer principal separation.
3. A positive saved registry cursor wakes through push after new work; no zero
   reset or polling fallback is used.
4. One worker completes claim, three-minute renewals, submit, and immediate
   re-arm; an independent reviewer closes it.
5. Recurring dispatch survives restart and reoffers expired work without losing
   persisted state or relying on stale hard pins.
6. O1 doctor, health, data-count, rollback-readiness, and no-mixed-JWKS checks
   pass; retain the rollback unit through the observation window.
7. The activated Home package, Quickstart, full inventory, and browser capture
   still hash-match the tagged `RELEASE_CANDIDATE_SHA` evidence.

T6 remains paused and outside this release. Any failed equality or observation
returns the train to a new immutable candidate; it is never waived as a release
subset.
