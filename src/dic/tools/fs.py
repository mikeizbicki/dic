"""Filesystem tools: what is where.

    dic --tools dic.tools.fs:* -m groq+qwen "what is in this directory?"

Nothing here is sandboxed and nothing here stops at a mount point: these
read the filesystem of the process that runs dic.
"""
import os


def ls(path: str = ".", hidden: bool = False) -> list[str]:
    """List what is in a directory, sorted, directories with a trailing '/'.

    Call this to see what a directory holds before reading anything out of
    it.  The path is relative to the working directory or absolute; a path
    that does not exist is an error and not an empty listing.

    >>> import tempfile
    >>> with tempfile.TemporaryDirectory() as d:
    ...     os.mkdir(os.path.join(d, "sub"))
    ...     open(os.path.join(d, "b.txt"), "w").close()
    ...     open(os.path.join(d, ".secret"), "w").close()
    ...     ls(d), ls(d, hidden=True)
    (['b.txt', 'sub/'], ['.secret', 'b.txt', 'sub/'])
    """
    with os.scandir(path) as listing:
        entries = sorted(listing, key=lambda entry: entry.name)
    return [entry.name + ("/" if entry.is_dir() else "")
            for entry in entries if hidden or not entry.name.startswith(".")]
