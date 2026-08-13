"""`python -m chronos ...` -> the same CLI as the `chronos` console script.

One argument parser, not two. Defining subcommands here as well would mean two
places to keep in sync and two ways for them to disagree.
"""

from .cli import main

if __name__ == "__main__":
    main()
