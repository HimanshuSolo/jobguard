"""Resume package: structured loader + fabrication checker."""

from jobguard.resume.checker import check_card, keyword_gap
from jobguard.resume.loader import (
    Resume,
    ResumeItem,
    load_resume,
    resume_dict_from_text,
    write_resume_yaml,
)
from jobguard.resume.summary import clear_compact_cache, compact_resume

__all__ = [
    "Resume",
    "ResumeItem",
    "check_card",
    "clear_compact_cache",
    "compact_resume",
    "keyword_gap",
    "load_resume",
    "resume_dict_from_text",
    "write_resume_yaml",
]
