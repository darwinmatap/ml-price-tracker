import os

# app.database exige DATABASE_URL al importarse (fail-fast en producción).
# Para que la sola importación no rompa los tests que no tocan la DB real,
# se define un valor por defecto aquí, antes de que se importe cualquier
# módulo de la app. Los tests de modelos igual usan su propio motor SQLite
# en memoria (ver tests/test_models.py); esto nunca toca una base real.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
