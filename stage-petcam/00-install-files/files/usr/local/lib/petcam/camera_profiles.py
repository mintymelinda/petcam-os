"""Compatibility marker for upgrades from pre-0.2.3 PetCam installations.

The multi-stream camera profile implementation was removed in v0.2.3.
Older installed updaters still require this path to exist while validating an
update archive. The current single-stream application does not import this
module; keep it inert until those older updaters have aged out.
"""
