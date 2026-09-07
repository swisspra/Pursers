from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import stat
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ROOT.parents[1]
CENTRAL_SRC = REPOSITORY_ROOT / "packages" / "central" / "src"
sys.path.insert(0, str(CENTRAL_SRC))
sys.path.insert(0, str(ROOT))

import door_admin  # noqa: E402
from pursers_central.jwt_verifier import (  # noqa: E402
    JWTTokenVerifier,
    JWTVerifierConfig,
)


JWT_SHAPE = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)


class SimulatedCrash(RuntimeError):
    pass


class DoorAdminTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.temporary.name)
        self.jwks = self.root / "jwks.json"
        self.keys = self.root / "keys"
        self.central_url = "https://central.example.test:9443/mcp"
        self.now = int(time.time())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def issue(self, role: str, **overrides: object) -> door_admin.IssuedCredential:
        arguments: dict[str, object] = {
            "board": "project-a",
            "role": role,
            "central_url": self.central_url,
            "jwks_path": self.jwks,
            "keys_dir": self.keys,
            "now": self.now,
        }
        arguments.update(overrides)
        return door_admin.issue_credential(**arguments)  # type: ignore[arg-type]

    def verifier(self) -> JWTTokenVerifier:
        return JWTTokenVerifier(
            JWTVerifierConfig(
                issuer="https://central.example.test:9443",
                audience=self.central_url,
                jwks_path=self.jwks,
            )
        )

    def verify(self, token: str):
        return asyncio.run(self.verifier().verify_token(token))

    def test_worker_and_reviewer_tokens_verify_with_exact_claims(self) -> None:
        worker = self.issue("worker")
        reviewer = self.issue("reviewer")

        worker_access = self.verify(worker.token)
        reviewer_access = self.verify(reviewer.token)

        self.assertIsNotNone(worker_access)
        self.assertIsNotNone(reviewer_access)
        assert worker_access is not None and reviewer_access is not None
        self.assertEqual(worker.kid, "door-project-a-worker-v1")
        self.assertEqual(reviewer.kid, "door-project-a-reviewer-v1")
        self.assertEqual(worker_access.subject, "door:project-a:worker")
        self.assertEqual(reviewer_access.subject, "door:project-a:reviewer")
        self.assertEqual(worker_access.client_id, "door-project-a-worker")
        self.assertEqual(reviewer_access.client_id, "door-project-a-reviewer")
        self.assertEqual(worker_access.scopes, ["board:read", "board:write"])
        self.assertEqual(reviewer_access.scopes, ["board:read", "board:review"])
        self.assertEqual(worker_access.resource, self.central_url)
        self.assertEqual(worker.claims["aud"], self.central_url)
        self.assertEqual(worker.claims["resource"], self.central_url)
        self.assertEqual(worker.claims["nbf"], self.now - 60)
        for path in self.keys.glob("*.pem"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_issue_reuses_current_key_and_named_lead_is_supported(self) -> None:
        first = self.issue("worker")
        refreshed = self.issue("worker", now=self.now + 10)
        named = door_admin.issue_credential(
            board="project-a",
            role=None,
            named=True,
            subject="lead-one",
            scope="board:read board:write board:review",
            central_url=self.central_url,
            jwks_path=self.jwks,
            keys_dir=self.keys,
            now=self.now,
        )

        self.assertEqual(refreshed.kid, first.kid)
        self.assertEqual(named.kid, "door-project-a-named-v1")
        access = self.verify(named.token)
        self.assertIsNotNone(access)
        assert access is not None
        self.assertEqual(access.subject, "lead-one")
        self.assertEqual(
            access.scopes, ["board:read", "board:write", "board:review"]
        )

    def test_rotate_revokes_v1_accepts_v2_and_preserves_subject(self) -> None:
        version_one = self.issue("worker")
        version_two = self.issue("worker", rotate=True, now=self.now + 1)

        self.assertEqual(version_two.kid, "door-project-a-worker-v2")
        self.assertEqual(version_two.claims["sub"], version_one.claims["sub"])
        self.assertIsNone(self.verify(version_one.token))
        self.assertIsNotNone(self.verify(version_two.token))
        kids = [item["kid"] for item in json.loads(self.jwks.read_text())["keys"]]
        self.assertEqual(kids, ["door-project-a-worker-v2"])

    def test_door_string_round_trip_and_rejections(self) -> None:
        issued = self.issue("reviewer")

        decoded = door_admin.decode_door(issued.door_string)

        self.assertEqual(
            decoded,
            {
                "u": self.central_url,
                "b": "project-a",
                "r": "reviewer",
                "kid": issued.kid,
                "exp": self.now + 180 * 86_400,
            },
        )
        with self.assertRaisesRegex(door_admin.DoorAdminError, "start with"):
            door_admin.decode_door("wrong.value")
        with self.assertRaisesRegex(door_admin.DoorAdminError, "base64url"):
            door_admin.decode_door("prs1.%%")
        bad_json = base64.urlsafe_b64encode(b"[]").decode().rstrip("=")
        with self.assertRaisesRegex(door_admin.DoorAdminError, "exactly"):
            door_admin.decode_door("prs1." + bad_json)
        malformed = base64.urlsafe_b64encode(
            json.dumps({"u": "u", "b": "b", "r": "r", "t": "no-token"}).encode()
        ).decode().rstrip("=")
        with self.assertRaisesRegex(door_admin.DoorAdminError, "malformed token"):
            door_admin.decode_door("prs1." + malformed)

    def test_failed_atomic_replace_leaves_valid_jwks_and_no_temp_file(self) -> None:
        original = self.issue("worker")
        original_document = json.loads(self.jwks.read_text())

        def crash(_temporary: Path, _target: Path) -> None:
            raise SimulatedCrash("power lost before replace")

        with self.assertRaises(SimulatedCrash):
            self.issue("worker", rotate=True, before_jwks_replace=crash)

        self.assertEqual(json.loads(self.jwks.read_text()), original_document)
        self.assertIsNotNone(self.verify(original.token))
        self.assertEqual(list(self.root.glob(".jwks.json.*.tmp")), [])

    def test_list_and_decode_outputs_never_contain_jwt_shape(self) -> None:
        issued = self.issue("worker")
        for arguments in (
            ["list", "--jwks", str(self.jwks)],
            ["decode", issued.door_string],
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                door_admin.execute(door_admin.build_parser().parse_args(arguments))
            rendered = output.getvalue()
            self.assertIsNone(JWT_SHAPE.search(rendered))
            self.assertNotIn(issued.token, rendered)
        self.assertEqual(door_admin.list_doors(self.jwks)[0]["kid"], issued.kid)

    def test_revoke_kid_removes_only_requested_key(self) -> None:
        worker = self.issue("worker")
        reviewer = self.issue("reviewer")

        door_admin.revoke_kid(self.jwks, worker.kid)

        self.assertIsNone(self.verify(worker.token))
        self.assertIsNotNone(self.verify(reviewer.token))
        with self.assertRaisesRegex(door_admin.DoorAdminError, "unknown kid"):
            door_admin.revoke_kid(self.jwks, worker.kid)

    def test_issue_command_prints_only_one_door_string(self) -> None:
        arguments = door_admin.build_parser().parse_args(
            [
                "issue",
                "--board",
                "project-a",
                "--role",
                "worker",
                "--central-url",
                self.central_url,
                "--jwks",
                str(self.jwks),
                "--keys-dir",
                str(self.keys),
            ]
        )
        output = io.StringIO()
        with redirect_stdout(output):
            door_admin.execute(arguments)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("prs1."))
        self.assertIsNone(JWT_SHAPE.search(lines[0]))


if __name__ == "__main__":
    unittest.main()
