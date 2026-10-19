"""Run a notebook headlessly, in place, saving it after every cell (so a 10 h run that stops
half-way still has the outputs of every finished cell).

Usage:
    .venv\\Scripts\\python -m benchmark.run_notebook signals_evaluation.ipynb
    set SIGNALS_PROFILE=SMOKE  (cmd)  /  $env:SIGNALS_PROFILE="SMOKE"  (PowerShell)  to override the profile
"""

import sys
import time
from pathlib import Path

import nbformat
from nbclient import NotebookClient


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(__doc__)
        return 2
    path = Path(args[0])
    nb = nbformat.read(path, as_version=4)
    started = time.monotonic()

    def save(cell=None, cell_index=None, **_):
        nbformat.write(nb, path)
        if cell_index is not None:
            print(f"[{time.strftime('%H:%M:%S')}] cell {cell_index + 1}/{len(nb.cells)} done "
                  f"({(time.monotonic() - started) / 60:.1f} min)", flush=True)

    client = NotebookClient(nb, timeout=None, kernel_name=nb.metadata.get("kernelspec", {}).get("name", "python3"),
                            resources={"metadata": {"path": str(path.parent.resolve())}},
                            on_cell_executed=save)
    try:
        client.execute()
    finally:
        nbformat.write(nb, path)
    print(f"finished in {(time.monotonic() - started) / 60:.1f} min; outputs saved to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
