"""Decoder plugin registry — name → plugin class (ADR-003 discovery v1).

Filesystem scan / PyPI entry points land later (ADR-003 open questions);
for now built-ins register themselves at import of
``openwebrx_plus.plugins`` and the REST surface reflects whatever is
registered. The registry is deliberately dumb: no instantiation, no
lifecycle — sessions own their plugin instances.
"""

from __future__ import annotations

from typing import TypeVar

from .base import DecoderManifest, DecoderPlugin

P = TypeVar("P", bound=type[DecoderPlugin])


class DecoderRegistry:
    """Class-level registry of decoder plugin types."""

    _plugins: dict[str, type[DecoderPlugin]] = {}

    @classmethod
    def register(cls, plugin_cls: type[DecoderPlugin]) -> type[DecoderPlugin]:
        """Idempotent registration; returns the class for decorator use."""
        name = plugin_cls.manifest.name
        existing = cls._plugins.get(name)
        if existing is not None and existing is not plugin_cls:
            raise ValueError(
                f"decoder {name!r} already registered by {existing.__module__}"
            )
        cls._plugins[name] = plugin_cls
        return plugin_cls

    @classmethod
    def get(cls, name: str) -> type[DecoderPlugin] | None:
        return cls._plugins.get(name)

    @classmethod
    def create(cls, name: str) -> DecoderPlugin:
        plugin_cls = cls._plugins.get(name)
        if plugin_cls is None:
            raise KeyError(f"unknown decoder: {name!r}")
        return plugin_cls()

    @classmethod
    def manifests(cls) -> list[DecoderManifest]:
        return [p.manifest for p in cls._plugins.values()]


decoder_registry = DecoderRegistry
