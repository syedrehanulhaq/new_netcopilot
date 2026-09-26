"""
Redaction of credentials in device output before it leaves NetCopilot.

Tool results (e.g. "show configuration", "show running-config") can contain
passwords, keys and SNMP communities. They are scrubbed before being sent to
the LLM, stored in conversation memory, or saved in investigation history.

Covers Junos ($9$ / crypt strings, SECRET-DATA lines, set and curly syntax),
IOS XR (password/secret/key with type digits), Nokia SR OS classic
(password "..." hash2) and Aruba AOS-CX (ciphertext/plaintext). BGP
communities are intentionally NOT redacted — they matter for routing analysis.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "<redacted>"

_VALUE = r'("[^"\n]*"|\S+)'
_TYPE_TOKENS = r"(?:\d{1,2}|password|encrypted|ciphertext|plaintext|ascii-text|hexadecimal|hash2?|clear|cleartext|sha|md5|aes-?\d*|des)"

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # PEM private keys / certificates bodies
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
     "-----BEGIN PRIVATE KEY-----" + MASK + "-----END PRIVATE KEY-----"),
    # Junos marks secret lines: secret "$9$..."; ## SECRET-DATA
    (re.compile(r'(?m)"[^"\n]*"(\s*;\s*## SECRET-DATA)'), f'"{MASK}"\\1'),
    # Junos $9$ (reversible obfuscation) and crypt hashes anywhere in quotes
    (re.compile(r'"\$(?:9|1|2[aby]?|5|6|8)\$[^"\n]*"'), f'"{MASK}"'),
    # keyword [type ...] value — IOS XR, SR OS, AOS-CX, Junos set syntax
    (re.compile(
        r"(?im)\b(encrypted-password|authentication-key|pre-shared-key|shared-secret|secret|password|passwd|"
        r"key-string|md5-key|auth-password|priv-password|auth-key|psk)"
        rf"((?:\s+{_TYPE_TOKENS})*)\s+{_VALUE}"
    ), rf"\1\2 {MASK}"),
    # "key 7 xxx", "key ciphertext xxx" (keychains, RADIUS/TACACS); a bare "key 1" is left alone
    (re.compile(rf"(?im)\bkey((?:\s+(?:7|0|6|encrypted|ciphertext|plaintext))+)\s+{_VALUE}"), rf"key\1 {MASK}"),
    # SNMP communities: IOS XR / AOS-CX "snmp-server community X", Junos "set snmp community X"
    (re.compile(rf"(?im)^(\s*(?:set\s+)?snmp(?:-server)?\s+community)\s+{_VALUE}"), rf"\1 {MASK}"),
    # Junos curly syntax inside snmp { }: "community public {"  (policy communities have "members")
    (re.compile(r'(?im)^(\s*community)\s+("[^"\n]*"|[^\s{;]+)(?=\s*\{\s*$)'), rf"\1 {MASK}"),
    # SR OS: community "public" r version both
    (re.compile(r'(?im)^(\s*community)\s+("[^"\n]*")(?=\s+(?:r|rw|rwa|mgmt|vpls-mgmt)\b)'), rf"\1 {MASK}"),
]


def redact_text(text: str) -> tuple[str, int]:
    """Return (redacted_text, number_of_redactions)."""
    total = 0
    for pattern, replacement in _PATTERNS:
        text, n = pattern.subn(replacement, text)
        total += n
    return text, total


def redact_obj(obj: Any) -> tuple[Any, int]:
    """Redact every string inside dicts/lists. Returns (new_obj, count)."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        total = 0
        out = {}
        for k, v in obj.items():
            out[k], n = redact_obj(v)
            total += n
        return out, total
    if isinstance(obj, list):
        total = 0
        items = []
        for v in obj:
            item, n = redact_obj(v)
            items.append(item)
            total += n
        return items, total
    return obj, 0
