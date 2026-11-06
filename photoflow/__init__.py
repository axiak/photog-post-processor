"""photoflow - an agent-operable RAW -> select -> crop/adjust photo pipeline.

Design rules
------------
1. Every creative decision is stored in ``norm`` space (floats in [0, 1] of the
   source frame), never in pixels. A crop decided on an 800 px proof therefore
   renders identically on a 6240 px master.
2. Reviewing models only ever see small proofs. Numbers (histogram, clipping,
   sharpness) travel with the image because text tokens are far cheaper than
   pixels and make the model's verdict far better.
3. The RAW science (demosaic, lens correction, white balance, base tone) is
   darktable's job. The agent's job is geometry, look, and selection.
"""

__version__ = "0.1.0"
SCHEMA_VERSION = 1

__all__ = ["geometry", "recipe", "render", "measure", "review"]
