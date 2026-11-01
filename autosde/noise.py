"""Free rules for changed files that cost tokens but rarely hold reviewable defects."""

from pathlib import PurePosixPath

# Any path segment matching one of these marks tooling, build or vendored output.
DIRECTORIES = {
    ".idea": "IDE settings",
    ".vscode": "IDE settings",
    ".fleet": "IDE settings",
    "node_modules": "vendored dependencies",
    "vendor": "vendored dependencies",
    ".venv": "virtual environment",
    "venv": "virtual environment",
    "__pycache__": "compiled bytecode",
    "dist": "build output",
    "build": "build output",
    "out": "build output",
    "target": "build output",
    ".next": "build output",
    "coverage": "coverage report",
    "htmlcov": "coverage report",
}
FILES = {
    "package-lock.json": "lockfile",
    "yarn.lock": "lockfile",
    "pnpm-lock.yaml": "lockfile",
    "poetry.lock": "lockfile",
    "pipfile.lock": "lockfile",
    "uv.lock": "lockfile",
    "cargo.lock": "lockfile",
    "go.sum": "lockfile",
    "composer.lock": "lockfile",
    "gemfile.lock": "lockfile",
    ".ds_store": "OS metadata",
    "thumbs.db": "OS metadata",
}
SUFFIXES = {
    ".min.js": "minified code",
    ".min.css": "minified code",
    ".map": "source map",
    ".lock": "lockfile",
    ".csv": "data file",
    ".tsv": "data file",
    ".log": "log file",
    ".iml": "IDE settings",
}


def flag(path):
    """Why a path is probably not worth reviewing, or None."""
    parts = PurePosixPath(path).parts
    for part in parts[:-1]:
        if part in DIRECTORIES:
            return f"{DIRECTORIES[part]} ({part}/)"
    name = parts[-1].lower() if parts else ""
    if name in FILES:
        return FILES[name]
    for suffix, reason in SUFFIXES.items():
        if name.endswith(suffix):
            return reason
    return None
