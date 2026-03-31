from __future__ import annotations

from dataclasses import dataclass

from platform_core.auth.supabase_auth import extract_email_domain


@dataclass(frozen=True)
class DomainRule:
    auto_approve: bool = True
    auto_create_api_key: bool = True
    daily_job_limit: int = 20
    max_concurrent_jobs: int = 2


# Domain-specific rules.  The SQL trigger on auth.users is the single source
# of truth for is_approved / auto_approved flags.  These rules drive the
# Python-side behaviour (API key auto-creation and rate-limit defaults).
DOMAIN_RULES: dict[str, DomainRule] = {
    "shaperon.com": DomainRule(
        auto_approve=True,
        auto_create_api_key=True,
        daily_job_limit=20,
        max_concurrent_jobs=2,
    ),
}


def get_domain_rule(email: str) -> DomainRule | None:
    """Return the domain rule for *email*, or None if no rule matches."""
    domain = extract_email_domain(email)
    if domain is None:
        return None
    return DOMAIN_RULES.get(domain)


def is_auto_approve_domain(email: str) -> bool:
    """Check if the email domain has an auto-approve rule."""
    rule = get_domain_rule(email)
    return rule is not None and rule.auto_approve
