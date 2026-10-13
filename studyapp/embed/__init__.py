"""Deduplication, run as a separate CLI in its own virtualenv.

sentence-transformers drags in PyTorch, some two gigabytes of it. Nothing here is
ever imported by the application; it is a one-off pass over a finished bank.
"""
