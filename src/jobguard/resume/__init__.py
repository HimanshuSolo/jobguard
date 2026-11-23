"""Resume package: structured loader + fabrication checker."""

from jobguard.resume.checker import check_card, keyword_gap
from jobguard.resume.loader import (
    Resume,
    ResumeItem,
    load_resume,
    resume_dict_from_text,
    write_resume_yaml,
)

__all__ = [
    "Resume",
    "ResumeItem",
    "check_card",
    "keyword_gap",
    "load_resume",
    "resume_dict_from_text",
    "write_resume_yaml",
]
