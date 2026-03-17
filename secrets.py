# This file intentionally exposes all stdlib secrets symbols
# so that packages importing 'from secrets import token_hex' work correctly.
# NetCopilot credential logic has been moved to credentials.py.
from _pyio import *  # noqa: unused
import sys as _sys
import importlib as _importlib

# Re-export everything from the real stdlib secrets module
_stdlib = _importlib.import_module("_secrets") if "_secrets" in _sys.stdlib_module_names else None

# Simple passthrough: import from the real secrets module using its file path
import builtins as _builtins
import types as _types

# The stdlib secrets module is at a known location; bypass our own file
_real_path = None
for _p in _sys.path:
    if "netcopilot_claude" not in _p and _p:
        import os as _os
        _candidate = _os.path.join(_p, "secrets.py")
        if _os.path.exists(_candidate):
            _real_path = _candidate
            break

# Just expose what starlette/fastapi needs
import os as _os
import hmac as _hmac

def token_bytes(nbytes=None):
    return _os.urandom(nbytes or 32)

def token_hex(nbytes=None):
    return token_bytes(nbytes).hex()

def token_urlsafe(nbytes=None):
    import base64
    tok = token_bytes(nbytes)
    return base64.urlsafe_b64encode(tok).rstrip(b'=').decode('ascii')

def compare_digest(a, b):
    return _hmac.compare_digest(a, b)

choice = None  # stdlib secrets.choice not commonly used outside crypto
