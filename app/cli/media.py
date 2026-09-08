"""Command-line client for the stable tsumugu media job API."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from ..settings import get_settings


DEFAULT_BASE_URL = os.environ.get("TSUMUGU_URL", get_settings().service_url)


def request_json(method: str, path: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        DEFAULT_BASE_URL.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body)
        except json.JSONDecodeError:
            detail = {"detail": body or str(exc)}
        raise RuntimeError(json.dumps(detail, ensure_ascii=False)) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot reach tsumugu at {DEFAULT_BASE_URL}: {exc.reason}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Submit and inspect tsumugu media jobs")
    subparsers = parser.add_subparsers(dest="command", required=True)

    submit = subparsers.add_parser("submit")
    submit.add_argument("url")
    submit.add_argument("--format", choices=("mp3", "m4a", "flac"), default=get_settings().media.default_format)
    submit.add_argument("--path", default=get_settings().media.default_path, dest="save_path")
    submit.add_argument(
        "--split",
        choices=("auto", "none", "chapter_info", "silence_detection"),
        default=get_settings().media.split_policy,
        dest="split_policy",
    )
    submit.add_argument("--keep-original", action="store_true")
    submit.add_argument("--title", default="")

    status = subparsers.add_parser("status")
    status.add_argument("job_id", type=int)

    cancel = subparsers.add_parser("cancel")
    cancel.add_argument("job_id", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "submit":
            result = request_json(
                "POST",
                "/api/media/jobs",
                {
                    "url": args.url,
                    "format": args.format,
                    "save_path": args.save_path,
                    "split_policy": args.split_policy,
                    "keep_original": args.keep_original,
                    "title": args.title,
                },
            )
        elif args.command == "status":
            result = request_json("GET", f"/api/media/jobs/{args.job_id}")
        else:
            result = request_json("POST", f"/api/media/jobs/{args.job_id}/cancel", {})
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
