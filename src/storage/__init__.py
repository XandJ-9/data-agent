"""Persistence subsystem: dual-backend storage (SQLite / PostgreSQL).

Layout: database.py owns connections/transactions, schema.py (step 3) owns the
table definitions, repositories/ own one domain each and never open
transactions, store.py is the composition facade.
"""
from storage.store import Store

__all__ = ['Store']
