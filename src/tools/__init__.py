"""Tools the agent can call.

Intentionally empty of imports. `agent.workflows` imports `tools.vision`, and
`tools.context_tools` imports `agent.workflows`; if this module pulled in the
tool modules eagerly, that pair would deadlock into a circular import depending
on which side was imported first. The registry lives in `tools.registry`, which
only the engine needs.
"""
