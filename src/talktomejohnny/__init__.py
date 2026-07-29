"""Public TalkToMeJohnny package facade.

The implementation remains in :mod:`talktomeclaude` during the compatibility
window so existing imports and editable installations keep working.
"""

from talktomeclaude import __version__

__all__ = ["__version__"]
