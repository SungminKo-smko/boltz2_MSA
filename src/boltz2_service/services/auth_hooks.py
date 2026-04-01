from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from platform_core.auth.domain_rules import get_domain_rule
from platform_core.models.api_key import ApiKey
from platform_core.models.profile import Profile
from platform_core.security import create_api_key

logger = structlog.get_logger(__name__)


def on_user_authenticated(profile: Profile, db: Session) -> str | None:
    """Post-authentication hook: auto-create API key for approved domains.

    Returns the plaintext API key if one was created, otherwise None.
    The caller is responsible for committing the transaction.
    """
    rule = get_domain_rule(profile.email)
    if not rule or not rule.auto_create_api_key:
        return None

    existing_id = db.scalar(
        select(ApiKey.id).where(
            ApiKey.profile_id == profile.id,
            ApiKey.service == "boltz2",
            ApiKey.is_active.is_(True),
        ).limit(1)
    )
    if existing_id:
        logger.info("api_key_exists", profile_id=profile.id, email=profile.email)
        return None

    raw_key, key_hash = create_api_key(prefix="b2")
    api_key = ApiKey(
        profile_id=profile.id,
        service="boltz2",
        name="auto",
        key_hash=key_hash,
        daily_job_limit=rule.daily_job_limit,
        max_concurrent_jobs=rule.max_concurrent_jobs,
    )
    db.add(api_key)
    db.flush()

    logger.info(
        "api_key_auto_created",
        profile_id=profile.id,
        email=profile.email,
        api_key_id=api_key.id,
    )
    return raw_key
