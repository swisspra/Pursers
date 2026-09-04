from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import leak_scan


def test_posix_home_pattern_and_exemptions() -> None:
    # Real home path triggers hit
    hits = leak_scan.scan_line("file=/Users/swissp/work/repo")
    assert "posix_home" in hits

    # Documented synthetic fixtures are exempt
    assert leak_scan.scan_line("path=/Users/synthetic-user/work") == []
    assert leak_scan.scan_line("path=/Users/synthetic-account/work") == []
    assert leak_scan.scan_line("path=/Users/synthetic-a/work") == []
    assert leak_scan.scan_line("path=/Users/synthetic-b/work") == []
    assert leak_scan.scan_line("path=/Users/example/work") == []
    assert leak_scan.scan_line("path=/Users/private-account/work") == []


def test_linux_and_windows_home_patterns() -> None:
    assert "linux_home" in leak_scan.scan_line("/home/alice/secret")
    assert leak_scan.scan_line("/home/synthetic-user/secret") == []

    assert "windows_home" in leak_scan.scan_line(r"C:\Users\bob\secret")
    assert leak_scan.scan_line(r"C:\Users\synthetic-user\secret") == []


def test_jwt_pattern_and_synthetic_fixtures() -> None:
    # Unexempt JWT shape
    raw_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0"
    hits = leak_scan.scan_line(f"token = {raw_jwt}")
    assert "jwt" in hits

    # Documented synthetic fixture token lines are exempt
    assert leak_scan.scan_line(f"token = {raw_jwt} # pragma: allowlist secret") == []
    assert leak_scan.scan_line('Path(target.token_file).write_text("eyJhbGciOi.TOKEN_MUST_NOT_APPEAR.sig")') == []
    assert leak_scan.scan_line('Path(target.token_file).write_text("eyJhbGciOi.SECRET_PAYLOAD_CONTENT.sig")') == []
    assert leak_scan.scan_line('Path(target.token_file).write_text("eyJhbGciOi.TOP_SECRET_GOOD_JWT_PAYLOAD.sig")') == []


def test_bearer_token_pattern_and_exemptions() -> None:
    # Real bearer token
    hits = leak_scan.scan_line("Authorization: Bearer secret_access_token_1234567890")
    assert "bearer_token" in hits

    # Documented placeholders are exempt
    assert leak_scan.scan_line("Authorization: Bearer <placeholder>") == []
    assert leak_scan.scan_line("Authorization: Bearer [REDACTED]") == []
    assert leak_scan.scan_line("Authorization: Bearer example_bearer_token") == []
    assert leak_scan.scan_line("Authorization: Bearer dummy_access_token") == []

    # Documented synthetic test tokens are exempt
    assert leak_scan.scan_line("Authorization: Bearer ABCDEFGHIJKLMNOPQRSTUVWXYZ") == []
    assert leak_scan.scan_line("Authorization: Bearer TESTTOKEN_123456") == []
    assert leak_scan.scan_line("Authorization: Bearer synthetic-local-bearer") == []
    assert leak_scan.scan_line("Authorization: Bearer SECRET-INPUT-VALUE") == []
    assert leak_scan.scan_line("Authorization: Bearer pp4-unique-secret-token") == []


def test_private_keys_and_cloud_credentials() -> None:
    pem = "-----BEGIN PRIVATE KEY-----\nMIGHAgEAMBMGByqGSM49AgEGBSskZQQPOw==\n-----END PRIVATE KEY-----"
    assert "pem_private_key" in leak_scan.scan_line(pem)

    # AWS
    assert "aws_access_key_id" in leak_scan.scan_line("AKIAIOSFODNN7EXAMPLE")
    assert leak_scan.scan_line("AKIAABCDEFGHIJKLMNOP") == []  # exempt synthetic fixture
    assert "aws_secret_access_key" in leak_scan.scan_line("aws_secret_access_key = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'")

    # GCP
    assert "gcp_api_key" in leak_scan.scan_line("AIzaSyD-12345678901234567890123456789ab")
    assert "gcp_oauth_token" in leak_scan.scan_line("ya29.a0AfH6SMB1234567890123456789012345")

    # Azure
    assert "azure_client_secret" in leak_scan.scan_line("AZURE_CLIENT_SECRET = 'aB3~defghijklmnopqrs'")


def test_inline_exemptions() -> None:
    secret_line = "path = /Users/realuser/work"
    assert "posix_home" in leak_scan.scan_line(secret_line)

    assert leak_scan.scan_line(secret_line + " # pragma: allowlist secret") == []
    assert leak_scan.scan_line(secret_line + " # leak-scan: exempt") == []
    assert leak_scan.scan_line(secret_line + " # noqa: leak") == []


def test_scan_file_and_masked_output(tmp_path: Path) -> None:
    leak_file = tmp_path / "bad.py"
    leak_file.write_text("SECRET_USER_DIR = '/Users/alice/projects'\n", encoding="utf-8")

    violations = leak_scan.scan_file(leak_file, "bad.py")
    assert len(violations) == 1
    assert violations[0].filename == "bad.py"
    assert violations[0].line_number == 1
    assert violations[0].rule_name == "posix_home"

    # Verify output is masked and never includes matched secret
    output = violations[0].format_finding()
    assert output == "bad.py:1: [posix_home] <masked>"
    assert "alice" not in output


def test_operator_markers_loading(tmp_path: Path) -> None:
    markers = tmp_path / "operator_markers.txt"
    markers.write_text("custom_sensitive_marker_xyz\n# comment line\n", encoding="utf-8")

    custom_rules = leak_scan._load_operator_markers(markers)
    assert len(custom_rules) == 1
    assert custom_rules[0].pattern == "custom_sensitive_marker_xyz"

    assert "operator_marker" in leak_scan.scan_line(
        "found custom_sensitive_marker_xyz here", rules=custom_rules
    )


def test_cli_main_exit_codes(tmp_path: Path) -> None:
    good_file = tmp_path / "good.txt"
    good_file.write_text("clean text /Users/synthetic-user/repo\n", encoding="utf-8")

    bad_file = tmp_path / "bad.txt"
    bad_file.write_text("leaked path /Users/charlie/repo\n", encoding="utf-8")

    assert leak_scan.main([str(good_file)]) == 0
    assert leak_scan.main([str(bad_file)]) == 1
