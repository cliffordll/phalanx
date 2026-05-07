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
from tools import terminal_tool  # noqa: F401  # Phase 2.2 wave 1: minimal local backend used by file_tools
from tools import file_tools  # noqa: F401  # Phase 2.2 wave 1: read_file / write_file / patch / search_files
from tools import todo_tool  # noqa: F401  # Phase 2.2 wave 2: in-memory task list (per-AIAgent store)
from tools import web_tools  # noqa: F401  # Phase 2.2 wave 4: web_search / web_extract / web_crawl
from tools import delegate_tool  # noqa: F401  # Phase 2.8.c wave 1: spawn sub-agent
