"""letterstream-mcp: an MCP server for LetterStream, built around a mail gate.

Public entry points:

* :class:`~letterstream_mcp.toolset.ToolSet` - the tool functions themselves.
* :class:`~letterstream_mcp.gate.MailGate` - submit/authorize policy.
* :func:`~letterstream_mcp.config.load_config` - configuration resolution.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .config import Config, load_config
from .errors import (
    AmbiguousRelease,
    ApiError,
    ConfigError,
    CostCeilingExceeded,
    CredentialsError,
    DryRunRefusal,
    GateRefusal,
    LetterStreamError,
    LockTimeout,
    OrphanedJob,
    ProofExpired,
    ProofMismatch,
    TransportError,
    UnknownProof,
    ValidationError,
)
from .gate import MailGate
from .models import Address, Proof, Recipient, SubmitRequest
from .toolset import TOOL_NAMES, ToolSet

__all__ = [
    "TOOL_NAMES",
    "Address",
    "AmbiguousRelease",
    "ApiError",
    "Config",
    "ConfigError",
    "CostCeilingExceeded",
    "CredentialsError",
    "DryRunRefusal",
    "GateRefusal",
    "LetterStreamError",
    "LockTimeout",
    "MailGate",
    "OrphanedJob",
    "Proof",
    "ProofExpired",
    "ProofMismatch",
    "Recipient",
    "SubmitRequest",
    "ToolSet",
    "TransportError",
    "UnknownProof",
    "ValidationError",
    "__version__",
    "load_config",
]
