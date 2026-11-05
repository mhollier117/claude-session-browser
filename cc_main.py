"""PyInstaller entry point for cc.exe. launcher/cli.py uses package-relative imports, which fail when a frozen
build runs it as a top-level script, so the build starts here and imports the package normally."""
import sys

from launcher.cli import main

if __name__ == "__main__":
    sys.exit(main())
