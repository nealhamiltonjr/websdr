"""Plugins module — decoder plugin SDK + registry (ADR-003).

Importing this package registers the bundled decoders: the in-process
ADS-B plugin, the subprocess dump1090 plugin (slice-4.9), the in-process
AIS plugin (slice-6.4), the in-process dump978 UAT plugin (slice-9),
and the in-process CW (Morse code) plugin (slice-13).
"""

from .adsb import AdsbDecoderPlugin  # noqa: F401  (registers itself)
from .ais import AisDecoderPlugin  # noqa: F401  (registers itself)
from .base import (  # noqa: F401
    DecoderAlreadyAttached,
    DecoderAttachContext,
    DecoderAttachError,
    DecoderBinaryMissing,
    DecoderManifest,
    DecoderPlugin,
    TapPoint,
)
from .cw import CwDecoderPlugin  # noqa: F401  (registers itself)
from .dump978 import Dump978Plugin  # noqa: F401  (registers itself)
from .dump1090 import Dump1090Plugin  # noqa: F401  (registers itself)
from .registry import DecoderRegistry, decoder_registry  # noqa: F401
from .subprocess import (  # noqa: F401
    PluginRunner,
    SubprocessDecoderPlugin,
    SubprocessSpec,
    iq_to_bytes,
)

__all__ = [
    "AdsbDecoderPlugin",
    "AisDecoderPlugin",
    "CwDecoderPlugin",
    "DecoderAlreadyAttached",
    "DecoderAttachContext",
    "DecoderAttachError",
    "DecoderBinaryMissing",
    "DecoderManifest",
    "DecoderPlugin",
    "DecoderRegistry",
    "Dump1090Plugin",
    "Dump978Plugin",
    "PluginRunner",
    "SubprocessDecoderPlugin",
    "SubprocessSpec",
    "TapPoint",
    "decoder_registry",
    "iq_to_bytes",
]
