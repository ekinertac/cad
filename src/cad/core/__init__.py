"""
core/ — building blocks shared by every cad feature.

Anything in this directory is "infrastructure": pickers, parsers,
provider discovery, override sidecar I/O, and small utilities that
don't belong to any one feature. Feature modules import from here;
nothing in core/ may import from features/.

Layout overview lives in the project README under "Adding a provider"
and in the refactor branch's commit log.
"""
