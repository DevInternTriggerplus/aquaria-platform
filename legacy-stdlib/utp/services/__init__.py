"""Service layer.

Services are the enforcement boundary. Permission checks, tenant scoping,
capacity serialization and audit writing all happen here rather than in the API,
so that the HTTP layer, a partner integration, a scheduled job and a test all get
identical guarantees (R42.1, R42.10, R42.11).
"""
