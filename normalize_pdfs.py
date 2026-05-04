"""Normalize PDF files in a directory by re-distilling them with Ghostscript.

This rewrites each PDF through the ``pdfwrite`` device, which produces a clean,
consistently-structured PDF (compressed fonts, deduplicated images, no form
fields/bookmarks/JavaScript). The result is typically smaller and yields cleaner
text extraction downstream.

Usage (PowerShell):
    python normalize_pdfs.py <input_dir> [-o <output_dir>] [--exclude PATTERN]
                             [--gs <gswin64c.exe>] [--recursive] [--overwrite]

Examples:
    python normalize_pdfs.py "C:\\Users\\kilia\\Desktop\\Data\\sources"
    python normalize_pdfs.py ./in -o ./out --exclude Wasserzeichen --recursive
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


DEFAULT_GS_CANDIDATES = [
    "gswin64c",
    "gswin64c.exe",
    "gswin32c",
    "gswin32c.exe",
    "gs",
]


def find_ghostscript(explicit: str | None = None) -> str:
    """Return a path to a Ghostscript console executable, or raise."""
    if explicit:
        if Path(explicit).is_file():
            return explicit
        found = shutil.which(explicit)
        if found:
            return found
        raise FileNotFoundError(f"Ghostscript not found at {explicit!r}")

    for name in DEFAULT_GS_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found

    # Fall back to scanning the standard Windows install location.
    for base in (r"C:\Program Files\gs", r"C:\Program Files (x86)\gs"):
        base_path = Path(base)
        if not base_path.is_dir():
            continue
        for exe in base_path.rglob("gswin*c.exe"):
            return str(exe)

    raise FileNotFoundError(
        "Could not locate Ghostscript. Install it from https://ghostscript.com/ "
        "or pass --gs <path-to-gswin64c.exe>."
    )


def normalize_pdf(gs: str, src: Path, dst: Path, pdf_settings: str = "/default") -> None:
    """Re-distill ``src`` to ``dst`` via Ghostscript ``pdfwrite``."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        gs,
        "-dBATCH",
        "-dNOPAUSE",
        "-dQUIET",
        "-dSAFER",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.7",
        f"-dPDFSETTINGS={pdf_settings}",
        "-dDetectDuplicateImages=true",
        "-dCompressFonts=true",
        "-o",
        str(dst),
        str(src),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RuntimeError(f"Ghostscript failed for {src.name}: {msg}")


def iter_pdfs(input_dir: Path, recursive: bool, excludes: list[str]) -> list[Path]:
    pattern = "**/*.pdf" if recursive else "*.pdf"
    pdfs = sorted(p for p in input_dir.glob(pattern) if p.is_file())
    if excludes:
        pdfs = [p for p in pdfs if not any(ex.lower() in p.name.lower() for ex in excludes)]
    return pdfs


def human_kb(num_bytes: int) -> str:
    return f"{num_bytes / 1024:.0f} KB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input_dir", type=Path, help="Directory containing PDFs to normalize.")
    parser.add_argument(
        "-o", "--output-dir", type=Path, default=None,
        help="Output directory (default: <input_dir>_normalized).",
    )
    parser.add_argument(
        "--exclude", action="append", default=[],
        help="Skip files whose name contains this substring (case-insensitive). May be repeated.",
    )
    parser.add_argument("--gs", default=None, help="Path to Ghostscript console executable.")
    parser.add_argument("--recursive", action="store_true", help="Recurse into subdirectories.")
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Re-normalize files even if the destination already exists.",
    )
    parser.add_argument(
        "--pdf-settings", default="/default",
        choices=["/screen", "/ebook", "/printer", "/prepress", "/default"],
        help="Ghostscript -dPDFSETTINGS preset (default: /default).",
    )
    args = parser.parse_args(argv)

    input_dir: Path = args.input_dir
    if not input_dir.is_dir():
        print(f"Error: input_dir not found: {input_dir}", file=sys.stderr)
        return 2

    output_dir: Path = args.output_dir or input_dir.with_name(input_dir.name + "_normalized")
    if output_dir.resolve() == input_dir.resolve():
        print("Error: output_dir must differ from input_dir.", file=sys.stderr)
        return 2

    try:
        gs = find_ghostscript(args.gs)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    pdfs = iter_pdfs(input_dir, args.recursive, args.exclude)
    if not pdfs:
        print(f"No PDFs found in {input_dir}.")
        return 0

    print(f"Ghostscript: {gs}")
    print(f"Input  : {input_dir}")
    print(f"Output : {output_dir}")
    print(f"Files  : {len(pdfs)}")
    print()

    failed: list[tuple[Path, str]] = []
    for pdf in pdfs:
        rel = pdf.relative_to(input_dir)
        dst = output_dir / rel
        if dst.exists() and not args.overwrite:
            print(f"SKIP  {rel}  (exists)")
            continue
        try:
            normalize_pdf(gs, pdf, dst, args.pdf_settings)
        except RuntimeError as exc:
            print(f"FAIL  {rel}  -- {exc}")
            failed.append((pdf, str(exc)))
            continue
        before = pdf.stat().st_size
        after = dst.stat().st_size
        print(f"OK    {rel}  ({human_kb(before)} -> {human_kb(after)})")

    print()
    print(f"Done. {len(pdfs) - len(failed)} succeeded, {len(failed)} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
