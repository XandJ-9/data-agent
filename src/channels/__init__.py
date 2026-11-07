"""Channel gateway subsystem: external chat-protocol adapters (Feishu first).

Adapters own verification, command parsing and durable ingest; they call
storage repositories inside a single transaction and never execute the
Agent loop themselves.
"""
