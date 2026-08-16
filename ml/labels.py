"""Canonical Aurum label normalization.

Every source dataset ships its own vocabulary. This module is the only place a
source label is allowed to become an Aurum class, driven entirely by
`configs/aurum_labels.yaml`.

The important property is that normalization is *total*: a source label either
maps to an Aurum class or is explicitly listed as dropped with a reason. A label
that is in neither list raises, because a dataset that silently loses labels is
indistinguishable from one that never had them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "configs" / "aurum_labels.yaml"

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_key(label: str) -> str:
    """Collapse a source label to its matching key.

    "RAM stick", "ram_stick", "Ram-Stick" and "  RAM   STICK " all become
    "ram_stick", so a mapping entry only has to be written once.
    """
    return _NON_ALNUM.sub("_", label.strip().lower()).strip("_")


class UnknownLabelError(KeyError):
    """A source label is neither mapped nor explicitly dropped."""


@dataclass
class LabelMap:
    version: str
    classes: list[str]
    definitions: dict[str, str]
    _to_aurum: dict[str, str] = field(repr=False, default_factory=dict)
    _dropped: dict[str, str] = field(repr=False, default_factory=dict)

    @property
    def class_to_index(self) -> dict[str, int]:
        return {name: i for i, name in enumerate(self.classes)}

    def resolve(self, source_label: str) -> str | None:
        """Return the Aurum class for a source label, or None if it is dropped.

        Raises UnknownLabelError when the label has never been reviewed.
        """
        key = normalize_key(source_label)
        if key in self._to_aurum:
            return self._to_aurum[key]
        if key in self._dropped:
            return None
        raise UnknownLabelError(
            f"Source label {source_label!r} (key {key!r}) is not in "
            f"{CONFIG_PATH.name}. Add it under `mappings` if it is one of "
            f"{self.classes}, or under `drop` with a reason. Refusing to guess."
        )

    def drop_reason(self, source_label: str) -> str | None:
        return self._dropped.get(normalize_key(source_label))


def load_label_map(path: Path | str = CONFIG_PATH) -> LabelMap:
    path = Path(path)
    with path.open() as fh:
        raw = yaml.safe_load(fh)

    classes: list[str] = list(raw["aurum_classes"])

    to_aurum: dict[str, str] = {}
    for aurum_class, aliases in raw["mappings"].items():
        if aurum_class not in classes:
            raise ValueError(
                f"{path.name}: `mappings` defines {aurum_class!r} which is not "
                f"in `aurum_classes` {classes}"
            )
        # The class name itself is always an alias for itself.
        for alias in [aurum_class, *aliases]:
            key = normalize_key(alias)
            if key in to_aurum and to_aurum[key] != aurum_class:
                raise ValueError(
                    f"{path.name}: alias {alias!r} maps to both "
                    f"{to_aurum[key]!r} and {aurum_class!r}"
                )
            to_aurum[key] = aurum_class

    dropped = {normalize_key(k): str(v) for k, v in (raw.get("drop") or {}).items()}

    conflicts = set(to_aurum) & set(dropped)
    if conflicts:
        raise ValueError(
            f"{path.name}: label(s) {sorted(conflicts)} appear in both "
            f"`mappings` and `drop`"
        )

    return LabelMap(
        version=str(raw["version"]),
        classes=classes,
        definitions=dict(raw.get("definitions") or {}),
        _to_aurum=to_aurum,
        _dropped=dropped,
    )
