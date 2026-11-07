"""Repository base: repos take an open connection and never manage transactions.

Transaction boundaries live in the services layer (or the Store facade for
single-repo conveniences); a repository method must stay usable inside a
caller's transaction for multi-repo composition.
"""
