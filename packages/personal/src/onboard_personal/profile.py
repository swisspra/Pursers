"""Verified re-export of the Personal profile API."""

from __future__ import annotations

from .artifacts import import_verified_component

_profile = import_verified_component(
    "onboard-client",
    "onboard_client",
    "onboard_client.personal_profile",
    package_member="onboard_client/__init__.py",
    module_member="onboard_client/personal_profile.py",
)

DEFAULT_PORT = _profile.DEFAULT_PORT
LEGACY_OVERRIDE_ENV = _profile.LEGACY_OVERRIDE_ENV
PERSONAL_REVIEW_POLICY = _profile.PERSONAL_REVIEW_POLICY
PROFILE_ENV = _profile.PROFILE_ENV
PersonalContext = _profile.PersonalContext
PersonalProfile = _profile.PersonalProfile
PersonalProfileError = _profile.PersonalProfileError
ProfileSecurityError = _profile.ProfileSecurityError
bootstrap_personal_review_policy = _profile.bootstrap_personal_review_policy
central_environment = _profile.central_environment
default_profiles_root = _profile.default_profiles_root
doctor_identity_summary = _profile.doctor_identity_summary
ensure_personal_profile = _profile.ensure_personal_profile
load_personal_profile = _profile.load_personal_profile
profile_path_for_project = _profile.profile_path_for_project
read_capability = _profile.read_capability
resolve_personal_context = _profile.resolve_personal_context
rotate_personal_capability = _profile.rotate_personal_capability
select_personal_profile = _profile.select_personal_profile

__all__ = [
    "DEFAULT_PORT",
    "LEGACY_OVERRIDE_ENV",
    "PERSONAL_REVIEW_POLICY",
    "PROFILE_ENV",
    "PersonalContext",
    "PersonalProfile",
    "PersonalProfileError",
    "ProfileSecurityError",
    "bootstrap_personal_review_policy",
    "central_environment",
    "default_profiles_root",
    "doctor_identity_summary",
    "ensure_personal_profile",
    "load_personal_profile",
    "profile_path_for_project",
    "read_capability",
    "resolve_personal_context",
    "rotate_personal_capability",
    "select_personal_profile",
]
