"""Secret redaction: strip credentials before anything is written to disk.

The proxy records request and response bodies, and prompts routinely carry
API keys, bearer tokens and private keys. Left alone they land in
``events.jsonl`` in clear text, which makes a recording unsafe to share,
export or diff. This module removes them *before* the wire parsers ever see
the payload, so no downstream code can copy a secret into an event.

Everything here is pure and free of I/O so it is cheap to unit-test one
pattern at a time. ``Redactor`` bundles the built-in patterns with any
project-specific ones the user supplies via ``flightrec run --redact``.
"""

from __future__ import annotations

import os
import re
from typing import Iterable

# What a redacted value is replaced with. Chosen so it survives a round-trip
# through a JSON string value untouched (no quotes, no backslashes) and reads
# unmistakably in the viewer.
REDACTED = "«redacted»"

# Header names whose *entire* value is a credential. detect_provider only ever
# inspects header *names*, so scrubbing the values here never breaks routing.
AUTH_HEADER_NAMES = frozenset({
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "api-key",
    "x-goog-api-key",
    "x-goog-iam-authorization-token",
    "cookie",
    "set-cookie",
})

# Ordered longest/most-specific first so a broad rule never eats a token a
# narrower rule would have labelled. Each pattern matches a whole secret.
_DEFAULT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # PEM private key blocks (any flavour): DOTALL so multi-line bodies match.
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
               re.DOTALL),
    # JSON web tokens: header.payload.signature.
    re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"),
    # OpenAI / Anthropic keys: sk-, sk-proj-, sk-ant-... (hyphens allowed).
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    # GitHub tokens: ghp_/gho_/ghu_/ghs_/ghr_ and fine-grained github_pat_.
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    # Slack tokens.
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    # Google API keys.
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
    # AWS access key ids (long-term AKIA, temporary ASIA).
    re.compile(r"A(?:KIA|SIA)[0-9A-Z]{16}"),
    # Bearer tokens embedded in free text ("Authorization: Bearer <tok>").
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
)


def _compile_extra(pattern: str) -> re.Pattern[str]:
    """A user ``--redact`` argument as a regex, falling back to a literal.

    A project-specific secret is often a plain string, and a stray ``(`` in it
    must not raise; an invalid regex is treated as the literal text to redact.
    """
    try:
        return re.compile(pattern)
    except re.error:
        return re.compile(re.escape(pattern))


class Redactor:
    """Applies the built-in and user patterns to strings and header maps."""

    def __init__(self, extra_patterns: Iterable[str] | None = None,
                 enabled: bool = True):
        self.enabled = enabled
        self.patterns: list[re.Pattern[str]] = list(_DEFAULT_PATTERNS)
        for p in extra_patterns or []:
            if p:
                self.patterns.append(_compile_extra(p))

    @classmethod
    def from_env(cls, extra_patterns: Iterable[str] | None = None,
                 env: dict[str, str] | None = None) -> "Redactor":
        """Build a redactor, honouring the ``FLIGHTREC_NO_REDACT`` escape hatch."""
        env = env if env is not None else dict(os.environ)
        enabled = env.get("FLIGHTREC_NO_REDACT", "") not in ("1", "true", "yes")
        return cls(extra_patterns, enabled=enabled)

    def text(self, s: str) -> str:
        """Return ``s`` with every known secret replaced by ``REDACTED``."""
        if not self.enabled or not s:
            return s
        for pat in self.patterns:
            s = pat.sub(REDACTED, s)
        return s

    def headers(self, headers: dict[str, str]) -> dict[str, str]:
        """Redact auth-header values wholesale and scan the rest for secrets."""
        out: dict[str, str] = {}
        for k, v in headers.items():
            if not self.enabled:
                out[k] = v
            elif k.lower() in AUTH_HEADER_NAMES:
                out[k] = REDACTED
            else:
                out[k] = self.text(v) if isinstance(v, str) else v
        return out
