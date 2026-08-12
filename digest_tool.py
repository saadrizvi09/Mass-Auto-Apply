"""CLI for the digest hunter: discover Referral-Alert job digests from public
Telegram channels and (optionally) pipe them into the app's referral pipeline.

  py -3.11 digest_tool.py hunt            # preview new matching digests
  py -3.11 digest_tool.py hunt --ingest   # also ingest -> dashboard form cards
  py -3.11 digest_tool.py hunt --channels freshershunt fresherjobsadda
"""
import argparse
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))

from app.services import digest_hunter  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Hunt referral digests from Telegram previews.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hunt", help="Sweep channels for new matching digests.")
    h.add_argument("--ingest", action="store_true",
                   help="Ingest finds into the app (form cards / email pipeline).")
    h.add_argument("--channels", nargs="*", default=None,
                   help="Override the default channel list.")
    h.add_argument("--batch", default="2026", help="Your graduation batch (default 2026).")
    args = ap.parse_args()

    if args.cmd == "hunt":
        res = digest_hunter.hunt(channels=args.channels, my_batch=args.batch,
                                 ingest=args.ingest)
        print(res["message"])
        for e in res["errors"]:
            print("  ! ", e)
        for f in res["finds"]:
            print("\n" + "=" * 70)
            print("SOURCE:", f["source"])
            print(f["text"][:900])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
