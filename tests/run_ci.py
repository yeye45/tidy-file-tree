"""Run CI tests with synthetic fixtures independent of runner directory metadata."""

import os
from pathlib import Path
import sys
import unittest


if __name__ == '__main__':
    # Fixtures live in the checkout and temp directory. The application correctly
    # protects paths referenced by these variables during normal use. Remove the
    # runner metadata only for the tests and their subprocesses.
    for name in ('GITHUB_WORKSPACE', 'RUNNER_WORKSPACE', 'RUNNER_TEMP'):
        os.environ.pop(name, None)

    tests_dir = Path(__file__).resolve().parent
    unittest.main(module=None, argv=[
        sys.argv[0], 'discover', '-s', str(tests_dir), '-v', *sys.argv[1:],
    ])
