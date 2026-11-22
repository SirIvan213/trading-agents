"""Standard-library readers for the files the pipeline writes. They never take a lock,
never write, and never block a writer (open_shared)."""
