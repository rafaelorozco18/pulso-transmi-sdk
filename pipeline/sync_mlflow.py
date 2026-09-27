"""Compatibilidad: la sincronización con MLflow vive ahora en ``tracking.py``.

    python pipeline/sync_mlflow.py     # == python pipeline/tracking.py
"""

from tracking import sync_all
from common import connect

if __name__ == "__main__":
    with connect() as connection:
        print(sync_all(connection))
