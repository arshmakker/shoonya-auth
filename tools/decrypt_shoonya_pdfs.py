#!/usr/bin/env python3
"""Decrypt PAN-protected Shoonya/Finvasia PDFs into plain, unlocked copies.

Shoonya emails contract notes, margin reports and ledgers as PDFs locked with your
PAN in capitals. This asks for the PAN once (hidden input, never echoed, never
written anywhere) and writes unlocked copies to an output directory.

Usage
-----
    python3 tools/decrypt_shoonya_pdfs.py                    # ~/Downloads -> ~/Downloads/shoonya_decrypted
    python3 tools/decrypt_shoonya_pdfs.py --since 2026-08-24 # only recent files
    python3 tools/decrypt_shoonya_pdfs.py --text             # also dump .txt alongside
    python3 tools/decrypt_shoonya_pdfs.py FILE.pdf [FILE...]  # explicit files

The PAN is held in memory only for the life of the process. It is not printed,
logged, stored, or passed on the command line (so it stays out of your shell history).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import getpass
import re
import sys
from pathlib import Path

try:
    from pypdf import PdfReader, PdfWriter
except ImportError:
    sys.exit("pypdf is required:  pip install 'pypdf[crypto]'")

# Shoonya's filename conventions: contract notes, margin statements, ledgers, ROS.
DEFAULT_PATTERNS = ("CN_*.pdf", "MARGIN_*.pdf", "FINLGR_*.pdf", "ROS_*.pdf", "FinancialLedger*.pdf")

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


def prompt_for_pan() -> str:
    """Ask for the PAN without echoing it. Retries on an obviously malformed entry."""
    for attempt in range(3):
        pan = getpass.getpass("PAN (hidden, used only as the PDF password): ").strip().upper()
        if PAN_RE.match(pan):
            return pan
        if not pan:
            sys.exit("No PAN entered; nothing to do.")
        remaining = 2 - attempt
        print(
            "  That doesn't look like a PAN (expected 5 letters, 4 digits, 1 letter)."
            + (f" {remaining} attempt(s) left.\n" if remaining else "\n"),
            file=sys.stderr,
        )
    sys.exit("Could not read a valid PAN.")


def collect_files(args: argparse.Namespace) -> list[Path]:
    if args.files:
        return [Path(f).expanduser() for f in args.files]

    in_dir = Path(args.in_dir).expanduser()
    if not in_dir.is_dir():
        sys.exit(f"Input directory not found: {in_dir}")

    found: set[Path] = set()
    for pattern in args.pattern or DEFAULT_PATTERNS:
        found.update(in_dir.glob(pattern))

    if args.since:
        try:
            cutoff = _dt.datetime.strptime(args.since, "%Y-%m-%d").timestamp()
        except ValueError:
            sys.exit(f"--since must be YYYY-MM-DD, got {args.since!r}")
        found = {p for p in found if p.stat().st_mtime >= cutoff}

    return _drop_browser_duplicates(sorted(found))


_DUP_SUFFIX_RE = re.compile(r"^(?P<stem>.+?) \(\d+\)$")


def _drop_browser_duplicates(paths: list[Path]) -> list[Path]:
    """Discard 'name (1).pdf' when 'name.pdf' exists with the same size.

    Re-downloading the same attachment leaves these behind; decrypting both just
    produces two identical outputs.
    """
    by_name = {p.name: p for p in paths}
    kept = []
    for p in paths:
        m = _DUP_SUFFIX_RE.match(p.stem)
        if m:
            original = by_name.get(m.group("stem") + p.suffix)
            if original and original.stat().st_size == p.stat().st_size:
                continue
        kept.append(p)
    return kept


def decrypt_one(src: Path, out_dir: Path, pan: str, want_text: bool, force: bool) -> str:
    """Return a one-word status: 'ok', 'plain', 'skip', 'badpass', or 'error: ...'."""
    dst = out_dir / src.name
    if dst.exists() and not force:
        return "skip"

    try:
        reader = PdfReader(str(src))

        if reader.is_encrypted:
            # PasswordType.NOT_DECRYPTED == 0 means the password was rejected.
            if not reader.decrypt(pan):
                return "badpass"
            status = "ok"
        else:
            status = "plain"

        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        # Carry over metadata when present; harmless if absent.
        if reader.metadata:
            writer.add_metadata(reader.metadata)

        out_dir.mkdir(parents=True, exist_ok=True)
        with open(dst, "wb") as fh:
            writer.write(fh)

        if want_text:
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
            dst.with_suffix(".txt").write_text(text, encoding="utf-8")

        return status
    except Exception as exc:  # noqa: BLE001 - surface the reason per file, keep going
        return f"error: {type(exc).__name__}: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Decrypt PAN-locked Shoonya PDFs into unlocked copies.",
    )
    parser.add_argument("files", nargs="*", help="Specific PDFs (default: scan --in-dir)")
    parser.add_argument("--in-dir", default="~/Downloads", help="Where to look (default: ~/Downloads)")
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Where to write (default: <in-dir>/shoonya_decrypted)",
    )
    parser.add_argument("--pattern", action="append", help="Glob to match; repeatable")
    parser.add_argument("--since", help="Only files modified on/after this date (YYYY-MM-DD)")
    parser.add_argument("--text", action="store_true", help="Also write a .txt of each PDF's text")
    parser.add_argument("--force", action="store_true", help="Overwrite existing outputs")
    args = parser.parse_args()

    files = collect_files(args)
    if not files:
        print("No matching PDFs found.", file=sys.stderr)
        return 1

    out_dir = (
        Path(args.out_dir).expanduser()
        if args.out_dir
        else (files[0].parent if args.files else Path(args.in_dir).expanduser()) / "shoonya_decrypted"
    )

    print(f"Found {len(files)} PDF(s). Writing unlocked copies to: {out_dir}\n")

    pan = prompt_for_pan()
    print()

    tally: dict[str, int] = {}
    try:
        for src in files:
            status = decrypt_one(src, out_dir, pan, args.text, args.force)
            key = status.split(":")[0]
            tally[key] = tally.get(key, 0) + 1
            symbol = {"ok": "✓", "plain": "·", "skip": "-", "badpass": "✗"}.get(key, "!")
            note = {
                "ok": "unlocked",
                "plain": "was not encrypted, copied",
                "skip": "already done (use --force to redo)",
                "badpass": "PAN rejected for this file",
            }.get(key, status)
            print(f"  {symbol} {src.name}  —  {note}")
    finally:
        # Best-effort scrub; Python strings are immutable, so this only drops the reference.
        pan = "\0" * len(pan)
        del pan

    print()
    summary = ", ".join(f"{v} {k}" for k, v in sorted(tally.items()))
    print(f"Done: {summary}")
    if tally.get("badpass"):
        print(
            "\nSome files rejected the PAN. Shoonya occasionally uses a different password "
            "scheme on older statements — check the covering email for that file.",
            file=sys.stderr,
        )
    if out_dir.exists():
        print(f"Unlocked files: {out_dir}")
    return 0 if not tally.get("badpass") else 2


if __name__ == "__main__":
    raise SystemExit(main())
