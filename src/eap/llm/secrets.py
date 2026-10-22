"""
Per-tenant secrets (EAP-47).

Multi-tenant means multi-bill: an enterprise tenant wants model calls on THEIR vendor
account - their key, their spend dashboard, their rate limits. The platform key then
becomes what it always should have been: a default for tenants who bring nothing.

The Protocol keeps the question "where do secrets live?" out of every other module. Today
the answer is environment variables, which is the right answer for dev boxes and demos.
The day it becomes Vault or a cloud secrets manager, that backend is a sibling class here
and one new branch in get_secret_resolver() - callers never change. (A network-backed
resolver would also want async and TTL caching; that is the day this Protocol grows an
async method, not before.)

Naming: TENANT_<SLUG>_<NAME>, slug uppercased with dashes flattened to underscores -
tenant "acme-corp" + name "ANTHROPIC_API_KEY" reads TENANT_ACME_CORP_ANTHROPIC_API_KEY.
That flattening must never land two tenants on one variable - "acme_corp" and
"Acme-Corp" would both read acme-corp's key - so only slugs of lowercase letters,
digits and dashes get secrets at all. Any other slug resolves to nothing: the tenant
runs on platform defaults, and the log says why.
"""

import os
import re
from functools import lru_cache
from typing import Protocol

from dotenv import dotenv_values

from eap.core.config import get_settings
from eap.core.logging import get_logger

log = get_logger("eap.llm.secrets")

# No "_" and no uppercase can appear, so uppercasing and "-" -> "_" is reversible:
# one slug, one variable, no twins.
_MAPPABLE_SLUG = re.compile(r"[a-z0-9-]+")


def env_name(tenant_slug: str, name: str) -> str:
    """The variable holding this tenant's secret. Refuses slugs that could collide."""
    if not _MAPPABLE_SLUG.fullmatch(tenant_slug):
        raise ValueError(f"tenant slug '{tenant_slug}' cannot name secrets unambiguously")
    return f"TENANT_{tenant_slug.upper().replace('-', '_')}_{name}"


class SecretResolver(Protocol):
    """Answers one question: this tenant's value for this named secret, or None."""

    def get_secret(self, tenant_slug: str, name: str) -> str | None: ...


@lru_cache
def _dotenv() -> dict[str, str | None]:
    # Pydantic parses .env for Settings but never exports it to os.environ, so a worker
    # started from a plain shell would miss TENANT_* lines kept there. Read the same file
    # once; like Settings, an .env edit takes a restart - one rule for both.
    return dotenv_values(".env")


class EnvSecretResolver:
    """Process environment first, then the project's .env file. The dev/demo backend."""

    def get_secret(self, tenant_slug: str, name: str) -> str | None:
        try:
            key = env_name(tenant_slug, name)
        except ValueError:
            # Fail toward the platform default, never toward a neighbour's key: this
            # tenant simply has no secrets of its own until its slug is fixed.
            log.warning("tenant_secrets_unmappable_slug", tenant_slug=tenant_slug, secret=name)
            return None
        return os.environ.get(key) or _dotenv().get(key) or None


@lru_cache
def get_secret_resolver() -> SecretResolver:
    backend = get_settings().secret_backend
    if backend == "env":
        return EnvSecretResolver()
    raise ValueError(f"unknown secret_backend '{backend}'")
