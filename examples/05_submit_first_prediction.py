"""Envía la predicción del ciclo abierto con el modelo campeón (una pasada del pipeline).

Equivale a ``python pipeline/watch.py --once``: colector → inferencia (POST) →
desempeño → reentrenamiento. Si aún no existe un campeón, créalo antes con
``python pipeline/retraining.py``.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

PIPELINE = Path(__file__).resolve().parents[1] / "pipeline"

if __name__ == "__main__":
    sys.path.insert(0, str(PIPELINE))
    sys.argv = ["watch.py", "--once"]
    runpy.run_path(str(PIPELINE / "watch.py"), run_name="__main__")
