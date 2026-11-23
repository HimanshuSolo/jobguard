"""Tests for the multi-job digest splitter (synthetic shapes mirroring real mail)."""

from __future__ import annotations

from jobguard.sources.digest_split import (
    split_digest_jobs,
    split_indeed_jobs,
    split_linkedin_jobs,
)

LINKEDIN_DIGEST = """Expand your search

Recommendations based on your activity.

Remote jobs
https://www.linkedin.com/comm/jobs/search-results/?keywords=Software

Software Engineer Intern (Cross Project Tools)
Hitachi Rail
Singapore
View job: https://www.linkedin.com/comm/jobs/view/111/?trackingId=x

---------------------------------------------------------

Associate Security Researcher Intern
Trellix
Singapore
View job: https://www.linkedin.com/comm/jobs/view/222/?trackingId=y

---------------------------------------------------------
"""

INDEED_DIGEST = """Indeed Job Alert
20 new software engineer jobs (Remote)

Jobs 1-20 of 20 new jobs
See matching results on Indeed: https://in.indeed.com/jobs?q=x

Freelance Developer (SAP ABAP)
Karunashri Software - Remote
₹500 - ₹1,500 an hour
Easily apply
A development company in Jaipur delivering SAP work.
https://in.indeed.com/rc/clk/dl?jk=aaa&from=ja

Backend Engineer (Python)
Beta Systems - Bengaluru
Just posted
Build APIs with FastAPI and Postgres.
https://in.indeed.com/rc/clk/dl?jk=bbb&from=ja
"""


def test_linkedin_digest_splits() -> None:
    jobs = split_linkedin_jobs("jobs-noreply@linkedin.com", "subject", LINKEDIN_DIGEST)
    assert len(jobs) == 2
    assert (jobs[0].role, jobs[0].company, jobs[0].location) == (
        "Software Engineer Intern (Cross Project Tools)",
        "Hitachi Rail",
        "Singapore",
    )
    assert "jobs/view/111" in jobs[0].link
    assert jobs[1].company == "Trellix"
    assert all(j.platform == "linkedin" for j in jobs)


def test_indeed_digest_splits() -> None:
    jobs = split_indeed_jobs("donotreply@jobalert.indeed.com", "subject", INDEED_DIGEST)
    assert len(jobs) == 2
    assert (jobs[0].role, jobs[0].company, jobs[0].location) == (
        "Freelance Developer (SAP ABAP)",
        "Karunashri Software",
        "Remote",
    )
    assert "jk=aaa" in jobs[0].link
    assert jobs[1].company == "Beta Systems"
    # Salary lines must not become phantom jobs.
    assert all("₹" not in j.company for j in jobs)


def test_single_job_falls_back() -> None:
    body = "Title: SDE Intern\nCompany: Acme\nApply: https://x.example/1"
    jobs = split_digest_jobs("jobs-noreply@linkedin.com", "SDE jobs", body)
    assert len(jobs) == 1
    assert jobs[0].company == "Acme"


def test_unparseable_returns_empty() -> None:
    assert split_digest_jobs("a@b.example", "", "") == []
