"""Legba Maintenance Daemon.

Deterministic background maintenance service. No LLM. Runs continuously,
performing scheduled housekeeping tasks: event lifecycle decay, entity
garbage collection, fact temporal management, signal corroboration scoring,
data integrity verification, and metric collection.

Usage:
    python -m legba.maintenance
"""
