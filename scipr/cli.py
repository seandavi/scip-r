import argparse
import json
import sys

from .parser import build_index


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="scip-r",
        description="Emit a SCIP index for an R package's source tree, "
        "without invoking R.",
    )
    ap.add_argument("pkg_dir", help="Path to an R package root (has DESCRIPTION, R/)")
    ap.add_argument(
        "-o", "--output", default="index.scip", help="Output path (default: index.scip)"
    )
    ap.add_argument(
        "--stats", action="store_true", help="Print a short summary after indexing"
    )
    ap.add_argument(
        "--emit-positions",
        metavar="PATH",
        help="Write guessed (unresolved) call-site positions as JSON, for "
        "a languageserver-based resolution pass to consume "
        "(see .github-action/ls_index.R)",
    )
    args = ap.parse_args(argv)

    positions: list = [] if args.emit_positions else None
    index = build_index(args.pkg_dir, positions_out=positions)

    with open(args.output, "wb") as f:
        f.write(index.SerializeToString())

    if args.emit_positions:
        with open(args.emit_positions, "w") as f:
            json.dump(positions, f, indent=2)
        if args.stats:
            print(f"{args.emit_positions}: {len(positions)} guessed positions", file=sys.stderr)

    if args.stats:
        n_docs = len(index.documents)
        n_occ = sum(len(d.occurrences) for d in index.documents)
        n_syms = sum(len(d.symbols) for d in index.documents)
        n_ext = len(index.external_symbols)
        print(
            f"{args.output}: {n_docs} documents, {n_syms} defined symbols, "
            f"{n_occ} occurrences, {n_ext} external symbols referenced",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
