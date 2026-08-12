"""Explainable, deterministic résumé-to-job fit scoring.

The scorer intentionally avoids claiming that a candidate will get a role.  It only
measures alignment between facts in the candidate's profile/parsed résumé and the text
of a saved job.  Scores are transient API output and are never written to the job row.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any


_TOKEN = re.compile(r"[a-z][a-z0-9+#.\-]{1,39}", re.IGNORECASE)
_STOPWORDS = frozenset(
    {
        "about", "after", "also", "and", "are", "been", "being", "but", "can",
        "company", "could", "from", "have", "into", "job", "more", "must", "our",
        "role", "should", "that", "the", "their", "them", "they", "this", "through",
        "using", "will", "with", "work", "working", "years", "you", "your",
    }
)

# Canonical skill -> phrases that safely identify it in ordinary résumé/JD text.
_SKILL_ALIASES: dict[str, tuple[str, ...]] = {
    "Python": ("python",),
    "Java": ("java",),
    "JavaScript": ("javascript", "js"),
    "TypeScript": ("typescript",),
    "React": ("react", "react.js", "reactjs"),
    "Angular": ("angular",),
    "Vue": ("vue", "vue.js", "vuejs"),
    "Node.js": ("node.js", "nodejs"),
    "FastAPI": ("fastapi",),
    "Django": ("django",),
    "Flask": ("flask",),
    "Spring": ("spring boot", "spring"),
    "C++": ("c++",),
    "C#": ("c#", "c sharp"),
    "Go": ("golang",),
    "Rust": ("rust",),
    "Ruby": ("ruby", "ruby on rails"),
    "PHP": ("php",),
    "Kotlin": ("kotlin",),
    "Swift": ("swift",),
    "SQL": ("sql",),
    "PostgreSQL": ("postgresql", "postgres"),
    "MySQL": ("mysql",),
    "MongoDB": ("mongodb", "mongo db"),
    "Redis": ("redis",),
    "AWS": ("aws", "amazon web services"),
    "Azure": ("azure",),
    "Google Cloud": ("google cloud platform", "google cloud", "gcp"),
    "Docker": ("docker",),
    "Kubernetes": ("kubernetes", "k8s"),
    "Terraform": ("terraform",),
    "Linux": ("linux",),
    "Git": ("git", "github", "gitlab"),
    "REST APIs": ("rest api", "restful"),
    "GraphQL": ("graphql",),
    "Machine learning": ("machine learning", "ml"),
    "Deep learning": ("deep learning",),
    "NLP": ("natural language processing", "nlp"),
    "LLMs": ("large language model", "large language models", "llm", "llms"),
    "Generative AI": ("generative ai", "genai", "gen ai"),
    "Pandas": ("pandas",),
    "NumPy": ("numpy",),
    "scikit-learn": ("scikit-learn", "sklearn"),
    "PyTorch": ("pytorch",),
    "TensorFlow": ("tensorflow",),
    "Spark": ("apache spark", "pyspark"),
    "Kafka": ("apache kafka", "kafka"),
    "Airflow": ("apache airflow", "airflow"),
    "dbt": ("dbt",),
    "Snowflake": ("snowflake",),
    "Power BI": ("power bi", "powerbi"),
    "Tableau": ("tableau",),
    "Excel": ("microsoft excel", "excel"),
    "Playwright": ("playwright",),
    "Selenium": ("selenium",),
    "HTML": ("html", "html5"),
    "CSS": ("css", "css3"),
    "Figma": ("figma",),
    "Product management": ("product management",),
    "Agile": ("agile",),
    "Scrum": ("scrum",),
}

_ROLE_SIGNALS: dict[str, frozenset[str]] = {
    "Backend engineer": frozenset(
        {"Python", "Java", "FastAPI", "Django", "Flask", "Spring", "Node.js", "Go", "SQL", "PostgreSQL", "Redis", "REST APIs"}
    ),
    "Frontend engineer": frozenset(
        {"JavaScript", "TypeScript", "React", "Angular", "Vue", "HTML", "CSS", "Figma"}
    ),
    "Full-stack engineer": frozenset(
        {"JavaScript", "TypeScript", "React", "Node.js", "Python", "Java", "SQL", "REST APIs"}
    ),
    "Data analyst": frozenset({"SQL", "Python", "Pandas", "Power BI", "Tableau", "Excel"}),
    "Data engineer": frozenset(
        {"Python", "SQL", "Spark", "Kafka", "Airflow", "dbt", "Snowflake", "AWS", "Google Cloud"}
    ),
    "Machine-learning engineer": frozenset(
        {"Python", "Machine learning", "Deep learning", "NLP", "LLMs", "Pandas", "scikit-learn", "PyTorch", "TensorFlow"}
    ),
    "Cloud / DevOps engineer": frozenset(
        {"AWS", "Azure", "Google Cloud", "Docker", "Kubernetes", "Terraform", "Linux", "Git"}
    ),
    "QA automation engineer": frozenset(
        {"Playwright", "Selenium", "Python", "Java", "JavaScript", "TypeScript"}
    ),
}


def _tokens(value: Any) -> set[str]:
    if not isinstance(value, str):
        return set()
    return {
        token.lower().strip(".-")
        for token in _TOKEN.findall(value)
        if len(token.strip(".-")) >= 3 and token.lower().strip(".-") not in _STOPWORDS
    }


def _contains_phrase(text: str, phrase: str) -> bool:
    normalized = " ".join(text.lower().split())
    escaped = re.escape(phrase.lower()).replace(r"\ ", r"\s+")
    return re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", normalized) is not None


def _detected_skills(text: str) -> set[str]:
    if not text:
        return set()
    return {
        canonical
        for canonical, aliases in _SKILL_ALIASES.items()
        if any(_contains_phrase(text, alias) for alias in aliases)
    }


def _profile_skills(profile: Mapping[str, Any]) -> set[str]:
    raw = profile.get("skills")
    values = raw if isinstance(raw, list) else []
    joined = "\n".join(value for value in values if isinstance(value, str))
    detected = _detected_skills(joined)
    # Preserve a concise user-entered skill even when it is outside the taxonomy.
    detected.update(
        value.strip()[:80]
        for value in values
        if isinstance(value, str) and 1 <= len(value.strip()) <= 80
    )
    return detected


def _target_roles(profile: Mapping[str, Any]) -> list[str]:
    preferences = profile.get("preferences")
    if not isinstance(preferences, Mapping):
        return []
    roles = preferences.get("target_roles")
    if not isinstance(roles, list):
        return []
    return [role.strip()[:120] for role in roles if isinstance(role, str) and role.strip()][:10]


def recommended_roles(profile: Mapping[str, Any] | None, resume_text: str | None) -> list[str]:
    """Return saved target roles, or a conservative skill-based direction."""

    safe_profile = profile if isinstance(profile, Mapping) else {}
    explicit = _target_roles(safe_profile)
    if explicit:
        return explicit[:5]
    candidate_skills = _profile_skills(safe_profile) | _detected_skills(resume_text or "")
    ranked = sorted(
        (
            (len(candidate_skills & signals) / len(signals), len(candidate_skills & signals), role)
            for role, signals in _ROLE_SIGNALS.items()
        ),
        reverse=True,
    )
    return [role for ratio, count, role in ranked if count >= 2 and ratio > 0][:3]


def _role_alignment(title: str, roles: Sequence[str], resume_tokens: set[str]) -> float:
    title_tokens = _tokens(title)
    if not title_tokens:
        return 0.0
    best = 0.0
    for role in roles:
        role_tokens = _tokens(role)
        if not role_tokens:
            continue
        if title.strip().lower() in role.lower() or role.lower() in title.strip().lower():
            best = max(best, 1.0)
        else:
            best = max(best, len(title_tokens & role_tokens) / len(title_tokens | role_tokens))
    if not roles:
        best = min(1.0, len(title_tokens & resume_tokens) / max(1, len(title_tokens)))
    return best


def _fit_context(
    profile: Mapping[str, Any] | None,
    resume_text: str | None,
) -> dict[str, Any]:
    safe_profile = profile if isinstance(profile, Mapping) else {}
    clean_resume = resume_text if isinstance(resume_text, str) else ""
    profile_skill_set = _profile_skills(safe_profile)
    candidate_skills = profile_skill_set | _detected_skills(clean_resume)
    return {
        "profile": safe_profile,
        "candidate_skills": candidate_skills,
        "resume_tokens": _tokens(clean_resume) | _tokens(" ".join(profile_skill_set)),
        "roles": _target_roles(safe_profile),
        "evaluated": bool(clean_resume.strip() or candidate_skills),
    }


def score_job_fit(
    job: Mapping[str, Any],
    *,
    profile: Mapping[str, Any] | None,
    resume_text: str | None,
    _context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Score one job using explainable text/skill alignment only."""

    context = _context or _fit_context(profile, resume_text)
    candidate_skills = set(context.get("candidate_skills") or set())
    resume_tokens = set(context.get("resume_tokens") or set())
    roles = context.get("roles") if isinstance(context.get("roles"), list) else []
    evaluated = context.get("evaluated") is True
    if not evaluated:
        return {
            "evaluated": False,
            "score": None,
            "label": "Résumé needed",
            "matched_skills": [],
            "missing_skills": [],
            "basis": "Upload and parse a résumé to measure alignment.",
        }

    title = str(job.get("title") or "")
    description = str(job.get("description") or "")
    job_text = f"{title}\n{description}"
    job_skills = _detected_skills(job_text)
    matched_skills = sorted(job_skills & candidate_skills)
    missing_skills = sorted(job_skills - candidate_skills)

    job_tokens = _tokens(job_text)
    overlap = len(job_tokens & resume_tokens) / max(6, min(30, len(job_tokens)))
    overlap = min(1.0, overlap)
    if job_skills:
        skill_alignment = len(matched_skills) / len(job_skills)
    else:
        skill_alignment = overlap
    role_alignment = _role_alignment(title, roles, resume_tokens)
    score = round(100 * ((0.55 * skill_alignment) + (0.30 * role_alignment) + (0.15 * overlap)))
    score = max(0, min(100, score))
    label = (
        "Strong alignment" if score >= 75 else
        "Good alignment" if score >= 55 else
        "Partial alignment" if score >= 35 else
        "Low alignment"
    )
    if matched_skills:
        basis = f"Matches {len(matched_skills)} detected skill{'s' if len(matched_skills) != 1 else ''}"
        if role_alignment >= 0.6:
            basis += " and your target-role direction."
        else:
            basis += "; review the remaining requirements."
    elif role_alignment >= 0.6:
        basis = "The title aligns with your target roles, but no explicit skill match was detected."
    else:
        basis = "Based on limited keyword overlap; review the full description before applying."
    return {
        "evaluated": True,
        "score": score,
        "label": label,
        "matched_skills": matched_skills[:8],
        "missing_skills": missing_skills[:8],
        "basis": basis,
    }


def enrich_jobs_with_fit(
    jobs: Sequence[Mapping[str, Any]],
    *,
    profile: Mapping[str, Any] | None,
    resume_text: str | None,
) -> list[dict[str, Any]]:
    """Return copies with a transient ``fit`` object; never mutate store rows."""

    context = _fit_context(profile, resume_text)
    return [
        {
            **dict(job),
            "fit": score_job_fit(
                job,
                profile=profile,
                resume_text=resume_text,
                _context=context,
            ),
        }
        for job in jobs
    ]


__all__ = ["enrich_jobs_with_fit", "recommended_roles", "score_job_fit"]
