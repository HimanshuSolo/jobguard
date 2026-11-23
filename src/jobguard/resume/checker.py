"""Fabrication checker (Phase 4) + keyword-gap analysis (Phase 8).

`check_card` deterministically verifies every tailoring suggestion cites
real resume IDs. `keyword_gap` compares a job's text against the resume's
skill inventory for the dashboard's per-job keyword chips.
"""

from __future__ import annotations

import re

from jobguard.llm.schemas import TailoringCard
from jobguard.resume.loader import Resume

_COMMON_TECH_TERMS = (
    "python sql fastapi react javascript typescript java go rust c++ aws gcp azure "
    "docker kubernetes postgres mysql mongodb redis git linux rest graphql django flask "
    "node express next.js tailwind figma tensorflow pytorch pandas numpy airflow spark "
    "kafka elasticsearch nginx ci/cd jenkins github actions terraform ansible "
    "microservices system design oop dsa operating systems dbms computer networks "
    "machine learning deep learning nlp computer vision llm rag langchain "
    "html css bootstrap redux vue angular svelte kotlin swift flutter dart "
    "spring springboot .net c# php laravel ruby rails scala haskell matlab r "
    "tableau powerbi excel selenium pytest jest unittest junit cypress playwright "
    "jira agile scrum postman swagger openapi firebase supabase vercel netlify "
    "graphql apollo trpc grpc websocket mqtt oauth jwt sso saml ldap vim "
    "bash powershell azure devops gcp cloud run ec2 s3 lambda cloudformation "
    "bigquery snowflake redshift databricks etl elt dbt looker metabase grafana "
    "prometheus datadog sentry new relic linear notion"
).split()


def _terms(resume: Resume) -> list[str]:
    seen: list[str] = []
    for item in resume.items:
        for chunk in (item.title, item.detail):
            for word in re.split(r"[^a-zA-Z+#./-]+", chunk):
                token = word.strip().casefold()
                if len(token) >= 2 and token not in seen:
                    seen.append(token)
    return seen


def keyword_gap(body: str, resume: Resume) -> tuple[list[str], list[str]]:
    """Split tech terms in ``body`` into (matched, missing) vs the resume."""
    haystack = body.casefold()
    resume_terms = set(_terms(resume))
    matched: list[str] = []
    missing: list[str] = []
    for term in _COMMON_TECH_TERMS:
        pattern = r"(?<![a-z0-9+#./-])" + re.escape(term) + r"(?![a-z0-9+#./-])"
        if not re.search(pattern, haystack):
            continue
        (matched if term in resume_terms else missing).append(term)
    return matched, missing


def check_card(card: TailoringCard, resume: Resume) -> list[str]:
    """Return violation messages; empty means the card is fabrication-free."""
    known = resume.known_ids()
    violations: list[str] = []
    for index, suggestion in enumerate(card.suggestions):
        where = f"suggestion[{index}] ({suggestion.kind})"
        unknown = [rid for rid in suggestion.resume_item_ids if rid not in known]
        if unknown:
            violations.append(f"{where} cites unknown resume IDs: {sorted(unknown)}")
        if suggestion.kind == "gap" and suggestion.resume_item_ids:
            violations.append(
                f"{where} is a GAP but cites resume IDs "
                f"{suggestion.resume_item_ids} — gaps must cite nothing"
            )
        if suggestion.kind in ("reword", "emphasize") and not suggestion.resume_item_ids:
            violations.append(
                f"{where} cites no resume IDs — reword/emphasize must cite at least one"
            )
    return violations
