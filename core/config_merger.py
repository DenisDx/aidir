"""
Config merging utilities.
Implements recursive config update with specific rules for lists and objects.
"""

from typing import Any, Dict, List, Union


def update_config(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """
    Update base config with override config.
    Mutates base dict in-place and returns it.
    
    Merge rules (recursive):
    - If value in override is a dict (object): recursively merge with base value (if dict)
    - If value in override is a list: replace base value with override list entirely
    - Otherwise: replace base value with override value
    
    Args:
        base: Base config dict to update
        override: Override config dict with new/replacement values
    
    Returns:
        Updated base dict
    """
    for key, override_value in override.items():
        if isinstance(override_value, dict):
            if key not in base:
                base[key] = {}
            if isinstance(base[key], dict):
                # Recursively merge dicts
                update_config(base[key], override_value)
            else:
                # Type mismatch: replace with override
                base[key] = override_value
        elif isinstance(override_value, list):
            # Lists are replaced entirely, not merged
            base[key] = override_value
        else:
            # Primitive values and other types: replace
            base[key] = override_value
    
    return base


def merge_contexts_top_level(saved: Dict[str, Any], task_context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merge saved context with task context (top-level only).
    Task context takes priority; missing keys from saved are preserved.
    
    Merge rules (top-level only, non-recursive):
    - If key exists in task_context: use task_context value (overwrites saved)
    - If key missing in task_context: keep saved value
    - Result is a new dict (non-mutating)
    
    Args:
        saved: Saved context from envid (base)
        task_context: Task-specific context overrides
    
    Returns:
        Merged context dict
    """
    result = dict(saved)  # Copy saved
    for key, value in task_context.items():
        result[key] = value
    return result
