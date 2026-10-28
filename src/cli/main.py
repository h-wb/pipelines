"""Read-only workspace CLI. No network, credential lookup, or runtime imports."""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from src.core.config import ConfigurationError, load_workspace
from src.integrations.registry import INTEGRATIONS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pipelines-workspace")
    parser.add_argument("--config", type=Path, default=Path("workspace.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("integrations", help="List bundled connector capabilities")
    commands.add_parser("validate", help="Validate desired configuration locally")
    commands.add_parser("connections", help="List validated connection bindings")
    args = parser.parse_args(argv)

    result: Any
    if args.command == "integrations":
        result = [
            {
                "name": definition.name,
                "input_modes": definition.input_modes,
                "input_format": definition.input_format,
                "capabilities": definition.capabilities,
                "credential_names": definition.credential_names,
                "resources": [asdict(resource) for resource in definition.resources],
                "settings_schema": definition.settings_model.model_json_schema(),
            }
            for definition in INTEGRATIONS.values()
        ]
    else:
        try:
            workspace = load_workspace(args.config)
        except ConfigurationError as error:
            print(f"Configuration error: {error}", file=sys.stderr)
            return 2
        if args.command == "validate":
            result = {
                "valid": True,
                "workspace": workspace.id,
                "revision": workspace.revision,
                "connection_count": len(workspace.connections),
            }
        else:
            result = [connection.summary() for connection in workspace.connections]
    print(json.dumps(result, indent=2))
    return 0
