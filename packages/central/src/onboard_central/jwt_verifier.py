"""RS256/JWKS TokenVerifier for On Board Personal Central."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier


@dataclass(frozen=True)
class JWTVerifierConfig:
    issuer: str
    audience: str
    jwks_path: Path
    clock_skew_s: int = 30
    algorithms: tuple[str, ...] = ("RS256",)

    def __post_init__(self) -> None:
        if not self.issuer or not self.audience:
            raise ValueError("issuer and audience must be non-empty")
        if self.clock_skew_s < 0 or self.clock_skew_s > 300:
            raise ValueError("clock_skew_s must be between 0 and 300")
        if self.algorithms != ("RS256",):
            raise ValueError("this verifier allows RS256 only")


class JWTTokenVerifier(TokenVerifier):
    """Validate bearer JWTs against a reloadable local JWKS, fail closed."""

    def __init__(self, config: JWTVerifierConfig):
        self.config = config

    def _verification_key(self, token: str) -> Any:
        header = jwt.get_unverified_header(token)
        if header.get("alg") not in self.config.algorithms:
            raise jwt.InvalidAlgorithmError("JWT algorithm is not allowed")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise jwt.InvalidKeyError("JWT kid is required")

        document = json.loads(self.config.jwks_path.read_text(encoding="utf-8"))
        keys = document.get("keys")
        if not isinstance(keys, list):
            raise jwt.InvalidKeyError("JWKS keys must be a list")
        matches = [item for item in keys if isinstance(item, dict) and item.get("kid") == kid]
        if len(matches) != 1:
            raise jwt.InvalidKeyError("JWT kid is unknown or ambiguous")
        jwk = matches[0]
        if jwk.get("kty") != "RSA" or jwk.get("use", "sig") != "sig":
            raise jwt.InvalidKeyError("JWT key is not an RSA signing key")
        if jwk.get("alg", "RS256") != "RS256":
            raise jwt.InvalidAlgorithmError("JWKS key algorithm is not RS256")
        return jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))

    @staticmethod
    def _scopes(claims: dict[str, Any]) -> list[str]:
        raw = claims.get("scope", "")
        if isinstance(raw, str):
            return [item for item in raw.split() if item]
        if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
            return list(dict.fromkeys(raw))
        raise jwt.InvalidTokenError("scope must be a string or list of strings")

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            key = self._verification_key(token)
            claims = jwt.decode(
                token,
                key=key,
                algorithms=list(self.config.algorithms),
                issuer=self.config.issuer,
                audience=self.config.audience,
                leeway=self.config.clock_skew_s,
                options={
                    "require": ["exp", "nbf", "iss", "sub", "aud", "resource", "scope"],
                    "strict_aud": True,
                },
            )
            if claims.get("resource") != self.config.audience:
                raise jwt.InvalidAudienceError("resource does not exactly match audience")
            subject = claims.get("sub")
            if not isinstance(subject, str) or not subject:
                raise jwt.InvalidSubjectError("sub must be a non-empty string")
            raw_client_id = claims.get("client_id")
            if raw_client_id is not None and (
                not isinstance(raw_client_id, str) or not raw_client_id
            ):
                raise jwt.InvalidTokenError("client_id must be a non-empty string when present")
            client_id = raw_client_id or "-"
            return AccessToken(
                token=token,
                client_id=client_id,
                scopes=self._scopes(claims),
                expires_at=int(claims["exp"]),
                resource=self.config.audience,
                subject=subject,
                claims=dict(claims),
            )
        except (jwt.PyJWTError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None
