"""Plugins module — decoder plugin SDK + registry (ADR-003).

Importing this package registers the bundled decoders: the in-process
ADS-B plugin and the subprocess dump1090 plugin (slice-4.9).
"""

from .adsb import AdsbDecoderPlugin  # noqa: F401  (registers itself)
from .base import (  # noqa: F401
    DecoderAlreadyAttached,
    DecoderAttachContext,
    DecoderAttachError,
    DecoderBinaryMissing,
    DecoderManifest,
    DecoderPlugin,
    TapPoint,
)
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
    "DecoderAlreadyAttached",
    "DecoderAttachContext",
    "DecoderAttachError",
    "DecoderBinaryMissing",
    "DecoderManifest",
    "DecoderPlugin",
    "DecoderRegistry",
    "Dump1090Plugin",
    "PluginRunner",
    "SubprocessDecoderPlugin",
    "SubprocessSpec",
    "TapPoint",
    "decoder_registry",
    "iq_to_bytes",
]
