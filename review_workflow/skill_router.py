from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SkillRoute:
    stage: str
    skills: tuple[str, ...]
    rationale: tuple[str, ...]


STAGE_ROUTES: dict[str, tuple[str, ...]] = {
    "scope": ("academic-research-suite", "Answering Research Questions", "gap-to-topic"),
    "search": ("nature-academic-search",),
    "citation_search": ("Traversing Citation Networks",),
    "biomedical_search": ("Searching Scientific Literature",),
    "rubric": ("Building Paper Screening Rubrics",),
    "screen": ("Evaluating Paper Relevance",),
    "fulltext": ("Finding Open Access Papers", "nature-downloader"),
    "read": ("nature-reader", "deeppapernote"),
    "reference_check": ("nature-ref-verifier", "citation-management"),
    "project_memory": ("research-context-compressor", "research-project-orienter"),
    "synthesis": ("literature-triage-matrix",),
    "write_zh": ("research-writing-skill", "scientific-writing", "nature-writing"),
    "write_en": ("nature-polishing",),
    "review": ("nature-reviewer", "academic-research-suite", "nature-ref-verifier"),
}


def route_skills(
    stage: str,
    *,
    paper_type: str | None = None,
    has_statistics: bool = False,
    has_structure_data: bool = False,
) -> SkillRoute:
    """Return the minimal installed-skill route for one stage or one paper.

    The read route always remains single-paper. Domain skills add an audit lens;
    they do not receive a second paper and do not regenerate the whole note.
    """

    if stage not in STAGE_ROUTES:
        raise ValueError(f"unknown stage: {stage}")
    skills = list(STAGE_ROUTES[stage])
    rationale = [f"stage:{stage}"]
    normalized = (paper_type or "").strip().lower()
    if stage == "read":
        if normalized in {"ml", "machine_learning", "xai", "algorithm"}:
            skills.extend(["ml-paper-writing", "scientific-writing"])
            rationale.append("ML/XAI method and claim audit")
        if normalized in {"materials", "chemistry", "battery", "electrochemistry"}:
            skills.append("scientific-writing")
            rationale.append("materials/battery evidence provenance")
        if normalized in {"review", "perspective", "survey"}:
            skills.append("literature-review")
            rationale.append("secondary-evidence and scope audit")
        if has_statistics:
            skills.extend(["nature-statistics", "statistical-analysis"])
            rationale.append("statistical reporting audit")
        if has_structure_data:
            skills.append("pymatgen")
            rationale.append("crystal/composition structure audit")
    unique = tuple(dict.fromkeys(skills))
    return SkillRoute(stage=stage, skills=unique, rationale=tuple(rationale))

