"""Run an approved maintenance stage for the database tier."""

import sys

from proxy import main


if __name__ == "__main__":
    sys.exit(main("db"))
