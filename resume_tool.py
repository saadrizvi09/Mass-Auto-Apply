"""CLI for the autonomous resume tailor+compile pipeline.

Usage:
  py -3.11 resume_tool.py tailor --jd jd.txt --company "EaseMyTrip" --role "GenAI Engineer" --out out.pdf
  echo "<jd text>" | py -3.11 resume_tool.py tailor --jd - --out out.pdf

Produces a JD-tailored, ATS-optimized one-page PDF using resume_base.tex + Tectonic.
Never fabricates: the tailor is constrained to the MASTER_SKILLS list.
"""
import argparse
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))

from app.services import resume_tailor  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Tailor + compile a resume to a job description.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("tailor", help="Tailor + compile a PDF for one JD.")
    t.add_argument("--jd", required=True, help="Path to a JD text file, or '-' for stdin.")
    t.add_argument("--company", default="", help="Company name (helps the tailor).")
    t.add_argument("--role", default="", help="Role title (helps the tailor).")
    t.add_argument("--out", required=True, help="Output PDF path.")
    t.add_argument("--show", action="store_true", help="Print the tailored summary + skills.")

    args = ap.parse_args()
    if args.cmd == "tailor":
        jd = sys.stdin.read() if args.jd == "-" else Path(args.jd).read_text(encoding="utf-8")
        if args.show:
            summ, skills = resume_tailor.tailor_blocks(jd, args.company, args.role)
            print("---- TAILORED SUMMARY ----\n" + summ)
            print("\n---- TAILORED SKILLS ----\n" + skills)
            tex = resume_tailor.build_tex(summ, skills)
            out = resume_tailor.compile_pdf(tex, Path(args.out))
        else:
            out = resume_tailor.tailor_and_compile(jd, args.company, args.role, args.out)
        print(f"\nPDF written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
