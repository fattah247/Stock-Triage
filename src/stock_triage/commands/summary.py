"""Summary context, validation, and generation commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..config import load_config
from ..logging_setup import setup_logging
from ..summary import (
    PROMPT_VERSION,
    build_summary_context,
    generate_summary,
    validate_summary_file,
)


def cmd_summary_context(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    context = build_summary_context(cfg, document_id=args.document_id)
    print(json.dumps(context.__dict__, indent=2, sort_keys=True))
    return 0


def cmd_summary_validate(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    result = validate_summary_file(
        cfg,
        document_id=args.document_id,
        input_path=Path(args.input_file),
        model_name=args.model_name,
        prompt_version=args.prompt_version,
    )
    print(
        "summary-validate: "
        f"analysis_run_id={result.analysis_run_id} "
        f"schema_valid={1 if result.schema_valid else 0} "
        f"evidence_link_valid={1 if result.evidence_link_valid else 0} "
        f"errors={','.join(result.errors) if result.errors else 'none'}"
    )
    return 0


def cmd_summary_generate(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg)

    result = generate_summary(
        cfg,
        document_id=args.document_id,
        prompt_version=args.prompt_version,
    )
    print(
        "summary-generate: "
        f"analysis_run_id={result.analysis_run_id} "
        f"schema_valid={1 if result.schema_valid else 0} "
        f"evidence_link_valid={1 if result.evidence_link_valid else 0} "
        f"errors={','.join(result.errors) if result.errors else 'none'}"
    )
    return 0


def register(sub) -> None:
    p_summary_context = sub.add_parser(
        "summary-context",
        help="Build curated facts and evidence for local summary prompts",
    )
    p_summary_context.add_argument("document_id", help="Document id to summarize")
    p_summary_context.set_defaults(func=cmd_summary_context)

    p_summary_validate = sub.add_parser(
        "summary-validate",
        help="Validate and persist a model output against Appendix C",
    )
    p_summary_validate.add_argument("document_id", help="Document id the summary refers to")
    p_summary_validate.add_argument("--input-file", required=True, help="Path to the model output JSON file")
    p_summary_validate.add_argument("--model-name", required=True, help="Local model name used to produce the output")
    p_summary_validate.add_argument(
        "--prompt-version",
        default=PROMPT_VERSION,
        help="Prompt version identifier for the validated output",
    )
    p_summary_validate.set_defaults(func=cmd_summary_validate)

    p_summary_generate = sub.add_parser(
        "summary-generate",
        help="Generate and validate a summary through the configured local LLM",
    )
    p_summary_generate.add_argument("document_id", help="Document id to summarize")
    p_summary_generate.add_argument(
        "--prompt-version",
        default=PROMPT_VERSION,
        help="Prompt version identifier for the generated output",
    )
    p_summary_generate.set_defaults(func=cmd_summary_generate)
