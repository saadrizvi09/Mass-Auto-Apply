from __future__ import annotations

from app.saas.matching import enrich_jobs_with_fit, recommended_roles, score_job_fit


def test_fit_scoring_is_explainable_and_prefers_supported_role() -> None:
    profile = {
        "skills": ["Python", "FastAPI", "PostgreSQL", "Docker"],
        "preferences": {"target_roles": ["Backend engineer"]},
    }
    resume = "Backend engineer building Python FastAPI APIs with PostgreSQL and Docker."
    backend = score_job_fit(
        {
            "title": "Backend Engineer",
            "description": "Build Python and FastAPI services backed by PostgreSQL and Docker.",
        },
        profile=profile,
        resume_text=resume,
    )
    frontend = score_job_fit(
        {
            "title": "Frontend Engineer",
            "description": "Build React, TypeScript, CSS and Figma interfaces.",
        },
        profile=profile,
        resume_text=resume,
    )

    assert backend["evaluated"] is True
    assert backend["score"] > frontend["score"]
    assert {"Python", "FastAPI", "PostgreSQL", "Docker"}.issubset(
        set(backend["matched_skills"])
    )
    assert "hiring" not in backend["basis"].lower()


def test_fit_requires_candidate_evidence_and_never_mutates_jobs() -> None:
    original = {"id": "job-1", "title": "Engineer", "description": "Build services."}
    enriched = enrich_jobs_with_fit([original], profile={}, resume_text=None)

    assert "fit" not in original
    assert enriched[0]["fit"] == {
        "evaluated": False,
        "score": None,
        "label": "Résumé needed",
        "matched_skills": [],
        "missing_skills": [],
        "basis": "Upload and parse a résumé to measure alignment.",
    }


def test_recommended_roles_prefers_user_reviewed_targets_then_skill_direction() -> None:
    assert recommended_roles(
        {"preferences": {"target_roles": ["Platform engineer", "Backend engineer"]}},
        "Python Docker",
    ) == ["Platform engineer", "Backend engineer"]

    inferred = recommended_roles(
        {"skills": ["Python", "SQL", "Pandas", "Tableau", "Excel"]},
        "",
    )
    assert "Data analyst" in inferred
