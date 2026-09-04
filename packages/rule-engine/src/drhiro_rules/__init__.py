"""drHiro deterministic rule engine.

Layer 1: pure calculations (rolling averages, trends, coverage).
Layer 2: versioned, jurisdiction-aware safety rules evaluated in pure
Python. The LLM never creates, changes, or suppresses these rules.
"""
