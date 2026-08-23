"""The item pipeline: frames to physical item lifecycles.

One physical object gets one identity here, and every later stage — load cell,
decision engine, routing, ledger — acts on that identity rather than minting
one of its own.

The load cell drives the machine: `PanMachine` runs the weighing cycle without
an operator action, and `SingleObjectZone` holds the identity while the object
is out of the camera's view on its way to the pan.
"""

from app.pipeline.association import SingleObjectZone
from app.pipeline.item_pipeline import ItemPipeline
from app.pipeline.pan import PanMachine, PanState
from app.pipeline.session import DemoSession

__all__ = [
    "DemoSession",
    "ItemPipeline",
    "PanMachine",
    "PanState",
    "SingleObjectZone",
]
