from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import leak_scan

# Non-exempt test candidates constructed from string fragments to keep test source clean
CANDIDATE_USER = "nonexempt" + "_candidate"
POSIX_HOME_CANDIDATE = "/Us" + f"ers/{CANDIDATE_USER}/work/repo"
LINUX_HOME_CANDIDATE = "/ho" + f"me/{CANDIDATE_USER}/secret"
WINDOWS_HOME_CANDIDATE = "C:\\Us" + f"ers\\{CANDIDATE_USER}\\secret"

RAW_JWT_CANDIDATE = (
    "ey" + "JhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
    "dozjgNryP4J3jVmNHl0w5N_XgL0"
)
BEARER_CANDIDATE = "Bea" + "rer " + "secret_access_token_1234567890"


def test_generic_home_patterns_and_exemptions() -> None:
    # Non-exempt candidates trigger violations
    assert "posix_home" in leak_scan.scan_line(f"file={POSIX_HOME_CANDIDATE}")
    assert "linux_home" in leak_scan.scan_line(LINUX_HOME_CANDIDATE)
    assert "windows_home" in leak_scan.scan_line(WINDOWS_HOME_CANDIDATE)

    # Documented synthetic fixture is exempt (synthetic-user only)
    assert leak_scan.scan_line("path=/Users/synthetic-user/work") == []
    assert leak_scan.scan_line("/home/synthetic-user/secret") == []
    assert leak_scan.scan_line(r"C:\Users\synthetic-user\secret") == []


def test_mixed_line_bypasses_are_prevented() -> None:
    # 1. Non-exempt home path + exempt example domain on the same line
    mixed_domain = f"clone from https://example.com into {POSIX_HOME_CANDIDATE}"
    hits_domain = leak_scan.scan_line(mixed_domain)
    assert "posix_home" in hits_domain

    # 2. Credential candidate + exempt loopback marker on the same line
    mixed_loopback = f"connect 127.0.0.1 with Authorization: {BEARER_CANDIDATE}"
    hits_loopback = leak_scan.scan_line(mixed_loopback)
    assert "bearer_token" in hits_loopback

    # 3. Non-exempt home path + exempt fixture token on the same line
    mixed_token = f"path {POSIX_HOME_CANDIDATE} contains Bearer ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    hits_token = leak_scan.scan_line(mixed_token)
    assert "posix_home" in hits_token


def test_jwt_pattern_and_synthetic_fixtures() -> None:
    hits = leak_scan.scan_line(f"token = {RAW_JWT_CANDIDATE}")
    assert "jwt" in hits

    # Inline exemption suppresses
    assert leak_scan.scan_line(f"token = {RAW_JWT_CANDIDATE} # pragma: allowlist jwt") == []

    # Documented synthetic fixture token lines are exempt
    assert leak_scan.scan_line('target = "eyJhbGciOi.TOKEN_MUST_NOT_APPEAR.sig"') == []
    assert leak_scan.scan_line('target = "eyJhbGciOi.SECRET_PAYLOAD_CONTENT.sig"') == []
    assert leak_scan.scan_line('target = "eyJhbGciOi.TOP_SECRET_GOOD_JWT_PAYLOAD.sig"') == []


def test_bearer_token_pattern_and_exemptions() -> None:
    hits = leak_scan.scan_line(f"Authorization: {BEARER_CANDIDATE}")
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
    pem = "-----BEG" + "IN PRIVATE KEY-----\nMIGHAgEAMBMGByqGSM49AgEGBSskZQQPOw==\n-----END PRIVATE KEY-----"
    assert "pem_private_key" in leak_scan.scan_line(pem)

    # AWS
    aws_id = "AK" + "IAIOSFODNN7EXAMPLE"
    assert "aws_access_key_id" in leak_scan.scan_line(aws_id)
    assert leak_scan.scan_line("AK" + "IAABCDEFGHIJKLMNOP") == []  # exempt synthetic fixture
    aws_secret = "aws_secret_access_key" + " = '" + "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'"
    assert "aws_secret_access_key" in leak_scan.scan_line(aws_secret)

    # GCP
    gcp_key = "AI" + "zaSyD-12345678901234567890123456789ab"
    assert "gcp_api_key" in leak_scan.scan_line(gcp_key)
    gcp_token = "ya" + "29.a0AfH6SMB1234567890123456789012345"
    assert "gcp_oauth_token" in leak_scan.scan_line(gcp_token)

    # Azure
    azure_secret = "AZURE_CLIENT_SECRET" + " = '" + "aB3~defghijklmnopqrs'"
    assert "azure_client_secret" in leak_scan.scan_line(azure_secret)


def test_inline_exemptions() -> None:
    secret_line = f"path = {POSIX_HOME_CANDIDATE}"
    assert "posix_home" in leak_scan.scan_line(secret_line)

    # Rule-scoped exemption suppresses only the named rule
    assert leak_scan.scan_line(secret_line + " # pragma: allowlist posix_home") == []
    assert leak_scan.scan_line(secret_line + " # leak-scan: exempt posix_home") == []
    assert leak_scan.scan_line(secret_line + " # noqa: leak posix_home") == []


def test_two_home_matches_with_one_exemption_fails() -> None:
    # Two home paths on the same line, one exemption marker
    second_candidate = "/Us" + f"ers/second_{CANDIDATE_USER}/work"
    line = f"{POSIX_HOME_CANDIDATE} and {second_candidate} # pragma: allowlist posix_home"
    hits = leak_scan.scan_line(line)
    assert hits == ["posix_home"]


def test_two_credential_matches_with_former_secret_marker_fails() -> None:
    # Two credential matches with the former 'secret' marker: both must still be detected
    line = f"token = {RAW_JWT_CANDIDATE}; auth = {BEARER_CANDIDATE} # pragma: allowlist secret"
    hits = leak_scan.scan_line(line)
    assert set(hits) == {"jwt", "bearer_token"}


def test_operator_marker_cannot_be_exempted(tmp_path: Path) -> None:
    # An operator marker plus attempted exemption marker: operator_marker cannot be exempted
    markers = tmp_path / "op_markers.txt"
    marker_name = "custom" + "_operator_ident"
    markers.write_text(f"{marker_name}\n", encoding="utf-8")
    custom_rules = leak_scan._load_operator_markers(markers)
    line = f"found {marker_name} # pragma: allowlist operator_marker"
    hits = leak_scan.scan_line(line, rules=custom_rules)
    assert hits == ["operator_marker"]


def test_two_rule_same_line_exemption_does_not_suppress_second_violation() -> None:
    # A line with both a home path and a raw JWT
    line_with_two_violations = (
        f"dir = '{POSIX_HOME_CANDIDATE}'; token = '{RAW_JWT_CANDIDATE}'"
    )
    both_hits = leak_scan.scan_line(line_with_two_violations)
    assert set(both_hits) == {"posix_home", "jwt"}

    # Marking posix_home exempt still flags jwt
    line_posix_exempt = line_with_two_violations + " # pragma: allowlist posix_home"
    hits_posix_exempt = leak_scan.scan_line(line_posix_exempt)
    assert hits_posix_exempt == ["jwt"]

    # Marking jwt exempt still flags posix_home
    line_jwt_exempt = line_with_two_violations + " # leak-scan: exempt jwt"
    hits_jwt_exempt = leak_scan.scan_line(line_jwt_exempt)
    assert hits_jwt_exempt == ["posix_home"]


def test_multiline_private_key_file_and_cli_detection(tmp_path: Path) -> None:
    pem_file = tmp_path / "private_key.pem"
    # Construct conventional multi-line PEM block dynamically
    pem_block = (
        "-----BEG" + "IN PRIVATE KEY-----\n"
        "MIGHAgEAMBMGByqGSM49AgEGBSskZQQPOw==\n"
        "-----END PRIVATE KEY-----\n"
    )
    pem_file.write_text(pem_block, encoding="utf-8")

    violations = leak_scan.scan_file(pem_file, "private_key.pem")
    assert len(violations) >= 1
    assert violations[0].filename == "private_key.pem"
    assert violations[0].line_number == 1
    assert violations[0].rule_name == "pem_private_key"
    assert violations[0].format_finding() == "private_key.pem:1: [pem_private_key] <masked>"

    assert leak_scan.main([str(pem_file)]) != 0


@pytest.mark.parametrize(
    "rel_path",
    [
        "tools/leak_scan.py",
        "tools/tests/test_leak_scan.py",
        "packages/central/src/pursers_central/scrub.py",
        "packages/import/scrub.py",
    ],
)
def test_second_leak_in_formerly_exempt_file_classes(rel_path: str, tmp_path: Path) -> None:
    # Proves no whole-file bypasses exist: an injected leak inside any file class is caught
    test_file = tmp_path / Path(rel_path).name
    content = f"# Valid code line 1\nLEAK_DIR = '{POSIX_HOME_CANDIDATE}'\n"
    test_file.write_text(content, encoding="utf-8")

    violations = leak_scan.scan_file(test_file, rel_path)
    assert len(violations) == 1
    assert violations[0].filename == rel_path
    assert violations[0].line_number == 2
    assert violations[0].rule_name == "posix_home"

    # Verify masked output format and non-zero CLI exit
    finding = violations[0].format_finding()
    assert finding == f"{rel_path}:2: [posix_home] <masked>"
    assert CANDIDATE_USER not in finding

    exit_code = leak_scan.main([str(test_file)])
    assert exit_code != 0


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
    bad_file.write_text(f"leaked path {POSIX_HOME_CANDIDATE}\n", encoding="utf-8")

    assert leak_scan.main([str(good_file)]) == 0
    assert leak_scan.main([str(bad_file)]) == 1
