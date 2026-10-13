"""Dependency-minimal YAML loader for unambiguous native callable declarations."""

from __future__ import annotations

import yaml

CI_INPUTS: tuple[str, ...] = ()


class UniqueLoader(yaml.SafeLoader):
    """Duplicate mapping keys cannot certify workflow behavior."""


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError("duplicate workflow key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)
