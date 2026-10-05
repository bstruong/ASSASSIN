"""MCP (Model Context Protocol) tool tiers for the ASSASSIN pipeline.

Tier 1 (local_tools): Raw SQL execution, raw PDF text extraction.
    Accessible only to local models.

Tier 2 (cloud_tools): Aggregation queries, schema introspection.
    Accessible to cloud models with restricted capabilities.
"""
