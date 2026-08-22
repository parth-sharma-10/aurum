"""The item pipeline: frames to physical item lifecycles.

One physical object gets one identity here, and every later stage — load cell,
decision engine, routing, ledger — acts on that identity rather than minting
one of its own.
"""

from app.pipeline.item_pipeline import ItemPipeline
from app.pipeline.session import DemoSession

__all__ = ["DemoSession", "ItemPipeline"]
