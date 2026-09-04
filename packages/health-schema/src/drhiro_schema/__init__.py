"""drHiro canonical health-data schema.

Single source of truth for metric types, recording methods, source
providers, and per-metric value JSON shapes. The API, worker, rule
engine, nutrition core, and Android bridge all import from here so the
canonical contract lives in one place.
"""
