from __future__ import annotations

import argparse
import json
import sys

from reference_contract import install_result_bundle_reference_contract


install_result_bundle_reference_contract()

from result_bundle import (  # noqa: E402
    cleanup_result_spool,
    load_expanded_result,
    publish_expanded_packet,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read expanded context from a previous Local RAG result bundle "
            "without running retrieval again."
        )
    )
    parser.add_argument("--result-set-id", required=True)
    parser.add_argument(
        "--item-id",
        action="append",
        default=[],
        help="Cached logical item ID; repeat at most three times.",
    )
    parser.add_argument(
        "--detail-level",
        choices=["expanded", "deep"],
        default="expanded",
    )
    parser.add_argument(
        "--result-delivery",
        choices=["file", "stdout"],
        default="file" if sys.platform.startswith("win") else "stdout",
    )
    args = parser.parse_args()
    maximum = 1 if args.detail_level == "deep" else 3
    if len(args.item_id) > maximum:
        parser.error(
            f"--detail-level {args.detail_level} accepts at most "
            f"{maximum} --item-id value(s)"
        )

    cleanup_result_spool()
    packet, expires_at = load_expanded_result(
        args.result_set_id,
        args.item_id,
        detail_level=args.detail_level,
    )
    if (
        args.result_delivery == "file"
        and packet.get("status") == "ok"
        and expires_at is not None
    ):
        output = publish_expanded_packet(
            packet,
            result_set_id=args.result_set_id,
            expires_at=expires_at,
        )
    else:
        output = packet
    print(
        json.dumps(
            output,
            ensure_ascii=args.result_delivery == "file",
            separators=(",", ":"),
        )
    )
    return 0 if packet.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
