"""Functional ISS engine for curryGPU.

The execution core is the native C++ coarse-boundary engine exposed through the
``native`` submodule (``launch`` / ``launch_words`` / ``step`` / ``state_diff``).
``native.available()`` is ``False`` until the compiled extension is built.
"""

from . import native

__all__ = ["native"]
