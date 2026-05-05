"""Tools package — registry + auto-loaded built-in tool modules.

Importing ``tools`` triggers each built-in tool module to register its
schema and handler with the singleton ``tools.registry.registry``.

Phase 1 only ships ``echo_tool``.  Later phases extend the manifest:

  Phase 2.2  → file_tools, file_operations, todo_tool, terminal_tool,
               path_security, binary_extensions, web_tools, interrupt,
               tool_output_limits, tool_result_storage
  Phase 7+   → registry.discover_builtin_tools() replaces the static
               list with AST-based auto-discovery.
"""

from tools import registry  # noqa: F401  -- exposes the singleton

# Self-registering tool modules.  Each import triggers a
# ``registry.register(...)`` call at the module's top level.
from tools import echo_tool  # noqa: F401
