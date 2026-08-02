"""Commands that compose more than one module.

Aegis and Mentor are peers: neither imports the other, so a command that must
READ one and WRITE the other has nowhere to live inside either. The transports
above them cannot own it either — the cardinal rule keeps authorization out of
`web/`. This package is that missing home, and it is deliberately narrow:
a module belongs here only when it genuinely spans modules.

The layering (enforced by `scripts/check_import_contracts.py`): workflows may
import `aegis`, `mentor`, and `core`; nothing below may import workflows.
"""
