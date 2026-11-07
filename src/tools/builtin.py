"""Generic built-in tool: bounded local statistics over user-provided numbers."""
import math

from tools.registry import ToolDefinition, ToolRegistry


def _calculate_statistics(args: dict) -> dict:
    values = args.get('values')
    if set(args) != {'values'} or not isinstance(values, list) or not 1 <= len(values) <= 1000:
        raise ValueError('invalid values')
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
        raise ValueError('non-finite values')
    total = math.fsum(values)
    if not math.isfinite(total):
        raise ValueError('overflow')
    return {'count': len(values), 'sum': total, 'mean': total / len(values),
            'min': min(values), 'max': max(values)}


def register_statistics(registry: ToolRegistry) -> None:
    """Register the only fully generic tool; domain tools live in their domains."""
    registry.register(ToolDefinition(
        name='calculate_statistics', description='对用户提供的有限数值计算数量、总和、均值、最小值和最大值。',
        parameters={'values': {'type': 'array', 'items': {'type': 'number'}, 'minItems': 1, 'maxItems': 1000}},
        required=['values'], handler=_calculate_statistics))
