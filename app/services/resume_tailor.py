"""Autonomous resume tailoring + compilation.

Given a job description, an LLM (Groq) rewrites ONLY the Summary + Technical-Skills
blocks of the base LaTeX resume to mirror the JD's language — constrained to a fixed
MASTER SKILL LIST so it can never fabricate a skill the candidate doesn't have. The
result is injected into resume_base.tex and compiled to a PDF with Tectonic (bundled
in .tools/), so the whole loop is offline-capable and needs no Overleaf.

Public API:
    tailor_and_compile(jd_text, company, role, out_pdf) -> Path
    tailor_blocks(jd_text, company, role) -> (summary_tex, skills_tex)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..config import ROOT
from ..integrations import groq_client
from ..profile import context_block, load_profile

BASE_TEX = ROOT / "resume_base.tex"

# The ONLY skills the tailor may use. The LLM selects/reorders from this universe and
# may NOT invent anything outside it. Everything here is genuinely on Saad's resume.
MASTER_SKILLS: dict[str, list[str]] = {
    "Languages": ["Python", "C++", "JavaScript", "TypeScript", "SQL"],
    "Generative AI": [
        "Large Language Models (LLMs)", "Retrieval-Augmented Generation (RAG)",
        "Agentic AI", "Multi-Agent Systems", "Prompt Engineering", "Multimodal AI",
        "LLM Orchestration", "LLM Evaluation",
    ],
    "NLP": ["NLP", "Sentiment Analysis (VADER)", "Dialogue Management", "Text Classification"],
    "LLM Platforms & Tooling": [
        "AWS Bedrock", "Groq", "Google Gemini", "Mistral", "LangChain",
        "AssemblyAI", "Pydantic",
    ],
    "Backend & Web": [
        "FastAPI", "Node.js", "Next.js", "React.js", "REST APIs", "WebSockets",
        "Microservices", "Server-Sent Events", "tRPC", "Prisma", "Tailwind CSS",
    ],
    "ML": ["Machine Learning", "Scikit-Learn", "Pandas", "HMM", "SVR"],
    "Cloud & DevOps": [
        "AWS (EC2, Bedrock, Elastic Beanstalk)", "Docker", "CI/CD",
        "GitHub Actions", "Redis", "BullMQ", "Git", "Postman",
    ],
    "Databases": ["PostgreSQL", "MySQL", "MongoDB", "Redis"],
    "CS Fundamentals": [
        "Data Structures & Algorithms", "System Design", "Object-Oriented Programming",
        "DBMS", "Operating Systems", "Computer Networks",
    ],
}

# A safe, generic default (used when offline / DRY_RUN / LLM parse fails).
DEFAULT_SUMMARY = (
    "AI Engineer and final-year B.Tech (ECE) building production LLM, RAG and agentic "
    "systems. Currently SDE Intern at Anything AI (AI on AWS Bedrock). Focused on the "
    "reliability layer---failover, validation, evals---that makes non-deterministic LLMs "
    "dependable in production."
)
DEFAULT_SKILL_LINES = [
    ("Languages", "Python, C++, JavaScript, TypeScript, SQL"),
    ("AI \\& ML", "Large Language Models (LLMs), Retrieval-Augmented Generation (RAG), Agentic AI, "
                  "Multi-Agent Systems, Prompt Engineering, Multimodal AI, LangChain, Scikit-Learn, Pandas"),
    ("LLM Platforms \\& Tooling", "AWS Bedrock, Groq, Google Gemini, Mistral, Pydantic"),
    ("Backend \\& Web", "FastAPI, Node.js, Next.js, React.js, REST APIs, WebSockets, Microservices"),
    ("Cloud \\& DevOps", "AWS (EC2, Elastic Beanstalk), Docker, CI/CD, GitHub Actions, Redis, Git"),
    ("Databases", "PostgreSQL, MySQL, MongoDB, Prisma"),
    ("CS Fundamentals", "Data Structures \\& Algorithms, System Design, OOP, DBMS, Operating Systems"),
]

_LATEX_ESC = {"&": r"\&", "%": r"\%", "#": r"\#", "_": r"\_", "$": r"\$"}


def _esc(text: str) -> str:
    """Escape LaTeX-special chars in LLM-produced plain text (leaves \\ alone)."""
    out = []
    for ch in text or "":
        out.append(_LATEX_ESC.get(ch, ch))
    return "".join(out)


def _master_flat() -> set[str]:
    return {s.lower() for items in MASTER_SKILLS.values() for s in items}


def _skills_block(skill_lines: list[tuple[str, str]]) -> str:
    lines = [f"    \\textbf{{{cat}}}: {items} \\\\" for cat, items in skill_lines if items]
    return "\n".join(lines)


def tailor_blocks(jd_text: str, company: str = "", role: str = "") -> tuple[str, str]:
    """Return (summary_tex, skills_tex) tailored to the JD. Falls back to safe defaults
    when offline/DRY_RUN or if the model output can't be parsed."""
    master_json = json.dumps(MASTER_SKILLS, ensure_ascii=False)
    system = (
        "You tailor a candidate's resume to a specific job description for maximum ATS "
        "keyword match. HARD RULES: (1) You may ONLY use skills that appear verbatim in the "
        "provided MASTER_SKILLS. NEVER invent, add, or imply any skill, tool, framework or "
        "experience not in that list. (2) The summary must be truthful to the candidate "
        "profile only. (3) Put skills the JD asks for FIRST. Output STRICT JSON only, no "
        "prose, in exactly this shape: "
        '{"summary": "<2-3 sentences, no first person, plain text>", '
        '"skill_lines": [{"category": "<short label>", "items": "<comma-separated skills '
        'from MASTER_SKILLS only>"}]}. Provide 6-7 skill_lines covering the candidate\'s '
        "relevant skills, ordered so JD-relevant categories/skills come first."
    )
    user = (
        f"CANDIDATE PROFILE:\n{context_block(load_profile())}\n\n"
        f"MASTER_SKILLS (the only allowed skills):\n{master_json}\n\n"
        f"TARGET ROLE: {role or '(unspecified)'} at {company or '(unspecified)'}\n\n"
        f"JOB DESCRIPTION:\n{jd_text[:4000]}\n\n"
        "Return the tailored JSON now."
    )
    raw = ""
    try:
        raw = groq_client.chat(system, user, temperature=0.3, max_tokens=700)
    except Exception:
        raw = ""

    summary, skill_lines = _parse_llm(raw)
    if not summary:
        summary = DEFAULT_SUMMARY
    if not skill_lines:
        skill_lines = DEFAULT_SKILL_LINES
    return _esc_summary(summary), _skills_block(skill_lines)


def _esc_summary(summary: str) -> str:
    # keep any intentional --- em-dash; escape the rest
    parts = summary.split("---")
    return "---".join(_esc(p) for p in parts)


def _parse_llm(raw: str) -> tuple[str, list[tuple[str, str]]]:
    if not raw:
        return "", []
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return "", []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return "", []
    allowed = _master_flat()
    summary = (data.get("summary") or "").strip()
    lines: list[tuple[str, str]] = []
    for row in data.get("skill_lines", []) or []:
        cat = _esc((row.get("category") or "").strip())
        items_raw = [s.strip() for s in (row.get("items") or "").split(",") if s.strip()]
        # keep ONLY skills that exist in MASTER_SKILLS (anti-fabrication gate)
        kept = [s for s in items_raw if s.lower() in allowed]
        if cat and kept:
            lines.append((cat, _esc(", ".join(kept))))
    return summary, lines


def _tectonic() -> str:
    env = os.getenv("TECTONIC_PATH")
    if env and Path(env).exists():
        return env
    local = ROOT / ".tools" / ("tectonic.exe" if os.name == "nt" else "tectonic")
    if local.exists():
        return str(local)
    found = shutil.which("tectonic")
    if found:
        return found
    raise FileNotFoundError("Tectonic not found (set TECTONIC_PATH or put it in .tools/).")


def build_tex(summary_tex: str, skills_tex: str) -> str:
    tpl = BASE_TEX.read_text(encoding="utf-8")
    return tpl.replace("%%SUMMARY%%", summary_tex).replace("%%SKILLS%%", skills_tex)


def compile_pdf(tex: str, out_pdf: Path) -> Path:
    out_pdf = Path(out_pdf)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        (tdp / "resume.tex").write_text(tex, encoding="utf-8")
        proc = subprocess.run(
            [_tectonic(), "-X", "compile", str(tdp / "resume.tex"), "--outdir", str(tdp)],
            capture_output=True, text=True, timeout=180,
        )
        pdf = tdp / "resume.pdf"
        if proc.returncode != 0 or not pdf.exists():
            raise RuntimeError(f"Tectonic compile failed: {proc.stderr[-500:]}")
        shutil.copyfile(pdf, out_pdf)
    return out_pdf


def tailor_and_compile(jd_text: str, company: str, role: str, out_pdf: str | Path) -> Path:
    summary_tex, skills_tex = tailor_blocks(jd_text, company, role)
    tex = build_tex(summary_tex, skills_tex)
    return compile_pdf(tex, Path(out_pdf))
