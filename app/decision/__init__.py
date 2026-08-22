"""Sorting policy: evidence and configuration to a bin.

`app.valuation` says what the evidence implies. This package says what the
machine does about it. Nothing here changes a cited or measured quantity.
"""

from app.decision.engine import Bin, Decision, ReasonCode, class_support, decide

__all__ = ["Bin", "Decision", "ReasonCode", "class_support", "decide"]
