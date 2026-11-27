"""Old name of the `consiglieri` package, kept only so a service installed before the rename keeps starting.  # rename-compat

The LaunchAgent and the home's launcher written by older versions run `python -m alfred run`, and their
venv's editable install still maps the `alfred` package to this folder. Nothing in Consiglieri imports it.
`consiglieri migrate` and `consiglieri service install` replace both; then this package can go
(docs/RENAME_PLAN.md, phase 4).
"""

from consiglieri import __version__  # noqa: F401
