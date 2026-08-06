"""Root conftest — ensures the project root is on sys.path so tests can import
main/files/internal_api without installing the project as a package."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
