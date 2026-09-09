# Independent Home acceptance sandbox handoff

Use `tools/home_acceptance_handoff.py` only after the final candidate has an
exact 40-character commit and a deterministic ZIP whose
`webui/candidate.json` names that commit. The preparer refuses production
boards, non-loopback origins, unapproved observer/host hashes, paths inside the
candidate checkout, symlinked inputs, or an existing output directory.

Every protected file and executable input is checked as supplied, before any
resolution step, so a symlink is refused even when it points at the approved
file and therefore hashes identically. Containment and identity checks then run
on the resolved path. Pass real paths: if a tool on `PATH` is itself a symlink,
resolve it first, for example
`--node "$(readlink -f "$(command -v node)")"`.

```sh
python3 tools/home_acceptance_handoff.py \
  --candidate-checkout /PATH/TO/FINAL/CANDIDATE \
  --commit FULL_40_CHARACTER_FINAL_SHA \
  --candidate-zip /PATH/TO/pursers-aionui-0.1.0.zip \
  --sandbox-root /PRIVATE/PATH/home-acceptance-handoff \
  --board sandbox-home-acceptance \
  --origin http://127.0.0.1:25808 \
  --observer-runner /PATH/TO/APPROVED/OBSERVER/runner.py \
  --observer-sha256 FINAL_TRUSTED_RUNNER_SHA256 \
  --observer-backend /PATH/TO/APPROVED/OBSERVER/browser_observer.py \
  --observer-backend-sha256 FINAL_TRUSTED_BACKEND_SHA256 \
  --observer-harness /PATH/TO/APPROVED/OBSERVER/harness.py \
  --observer-harness-sha256 FINAL_TRUSTED_HARNESS_SHA256 \
  --observer-install-dir /PRIVATE/VERIFIER/PATH/home-observer \
  --helper /PATH/TO/APPROVED/RUNTIME/host/helper.cjs \
  --helper-sha256 816be67dd9a6aa2e8385f5e6b8463a1a02623cdde0ff32d3c06c74e00fe5fbee \
  --node /ABSOLUTE/PATH/TO/node \
  --bridge-bin /ABSOLUTE/PATH/TO/pursers-wait-bridge \
  --aioncore-bin /ABSOLUTE/PATH/TO/aioncore \
  --ego-browser /ABSOLUTE/PATH/TO/ego-browser \
  --host-bundle /PATH/TO/SIGNED/AionUi.app \
  --host-cdhash cbd8ca92afa6ff19a38b95728b11c116734b68bd \
  --identity-mode webui \
  --runtime-commit f5a24301262bcbe56d993276362dc74d6f1728ad
```

The three observer source files must come from the final independently approved
integration successor, have its exact reviewed hashes, and share one source
directory. Never reuse hashes from a rejected or superseded candidate. The
preparer does not create `--observer-install-dir`; the
independent verifier creates it by running `reviewer-commands.sh`. The preparer
verifies the AionUi bundle identifier, version, Developer ID,
notarization, expected CDHash, and that AionCore is inside the signed bundle.
It restricts `--identity-mode` to `webui` or `aionpro`, records the selected
mode in `handoff.json`, and passes it explicitly to AionCore so the verifier's
signed-listener identity check cannot silently fall back to an unsigned mode.
It then installs the exact ZIP into an isolated extension directory. The output
directory and subdirectories are mode `0700`; metadata, assertions,
and the random helper token are mode `0600`. The token value is never printed
or copied into metadata. Run `start-aioncore.sh` and `start-helper.sh` in
separate terminals. The former sets only the isolated extension/data paths; the
latter binds the approved helper to the
selected sandbox board and exact loopback origin. `reviewer-commands.sh`
installs the verifier-owned observer, runs doctor, capture, validation, and the
live host gate. The independent verifier must replace each empty assertion
list and capture all nine required observations; absent lifecycle capabilities
remain explicit blockers, not waivers. `cleanup.sh` stops only PIDs
whose command line is bound to this handoff directory.

Operator authority is still required: sign in or pair an isolated AionUi
sandbox and issue its sandbox-only door directly to the independent verifier.
Do not place that door in this handoff, a ticket, a repository, or logs. The
preparer does not certify evidence and does not grant production access.
