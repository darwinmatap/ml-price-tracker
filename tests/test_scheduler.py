"""
Tests de app/scheduler.py:
- run_scan_all da el mismo resultado llamada directa que vía el endpoint
  (prueba que app/products.py no duplica la lógica, solo la reutiliza).
- _scan_all_job captura cualquier excepción de run_scan_all: se loguea
  pero nunca se propaga (no debe tumbar el proceso ni el test runner).
- El job queda registrado en el scheduler con la configuración correcta
  (interval de 1 hora, max_instances=1, coalesce=True) sin necesidad de
  iniciar el scheduler ni esperar tiempo real — solo se inspecciona la
  configuración del job.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

from apscheduler.triggers.interval import IntervalTrigger

from app.scheduler import SCAN_ALL_JOB_ID, _scan_all_job, create_scheduler, run_scan_all
from tests.conftest import make_ml_response


@patch("app.ml_client.requests.get")
def test_run_scan_all_directo_mismo_resultado_que_via_endpoint(mock_get, client, auth_headers, db_session):
    mock_get.return_value = make_ml_response(200, {"price": 500, "currency_id": "CLP", "title": "A"})
    crear_a = client.post(
        "/products",
        json={"url": "https://articulo.mercadolibre.cl/MLC-700000007-a"},
        headers=auth_headers,
    )
    mock_get.return_value = make_ml_response(200, {"price": 700, "currency_id": "CLP", "title": "B"})
    crear_b = client.post(
        "/products",
        json={"url": "https://articulo.mercadolibre.cl/MLC-700000008-b"},
        headers=auth_headers,
    )
    assert crear_a.status_code == 201 and crear_b.status_code == 201

    mock_get.side_effect = [
        make_ml_response(200, {"price": 555, "currency_id": "CLP", "title": "A"}),
        make_ml_response(404),
    ]

    resultado = run_scan_all(db_session)

    assert resultado == {
        "total": 2,
        "exitosos": 1,
        "fallidos": 1,
        "item_ids_fallidos": [crear_b.json()["item_id"]],
    }


@patch("app.ml_client.requests.get")
def test_scan_all_endpoint_mismo_resultado_que_run_scan_all_directo(mock_get, client, auth_headers):
    mock_get.return_value = make_ml_response(200, {"price": 500, "currency_id": "CLP", "title": "A"})
    crear_a = client.post(
        "/products",
        json={"url": "https://articulo.mercadolibre.cl/MLC-800000009-a"},
        headers=auth_headers,
    )
    mock_get.return_value = make_ml_response(200, {"price": 700, "currency_id": "CLP", "title": "B"})
    crear_b = client.post(
        "/products",
        json={"url": "https://articulo.mercadolibre.cl/MLC-800000010-b"},
        headers=auth_headers,
    )
    assert crear_a.status_code == 201 and crear_b.status_code == 201

    mock_get.side_effect = [
        make_ml_response(200, {"price": 555, "currency_id": "CLP", "title": "A"}),
        make_ml_response(404),
    ]

    resumen = client.post("/products/scan-all", headers=auth_headers).json()

    # Mismo shape y mismos valores que test_run_scan_all_directo_..., para
    # el mismo escenario (2 productos, uno ok, otro 404): confirma que
    # ambas rutas de entrada usan la misma lógica (run_scan_all), sin
    # duplicación.
    assert resumen == {
        "total": 2,
        "exitosos": 1,
        "fallidos": 1,
        "item_ids_fallidos": [crear_b.json()["item_id"]],
    }


def test_scan_all_job_captura_excepcion_y_no_propaga(caplog):
    with (
        patch("app.scheduler.SessionLocal") as mock_session_local,
        patch("app.scheduler.run_scan_all", side_effect=RuntimeError("la base de datos no responde")) as mock_run,
    ):
        mock_session = MagicMock()
        mock_session_local.return_value = mock_session

        with caplog.at_level("ERROR"):
            _scan_all_job()  # no debe lanzar RuntimeError ni ninguna otra excepción

        mock_run.assert_called_once_with(mock_session)
        # La sesión se abrió y se cerró pese al fallo (finally).
        mock_session.close.assert_called_once()

    errores = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errores) == 1
    assert "escaneo automático" in errores[0].message.lower()
    assert errores[0].severity == "high"


def test_scheduler_job_configurado_correctamente():
    scheduler = create_scheduler()
    job = scheduler.get_job(SCAN_ALL_JOB_ID)

    assert job is not None
    assert job.max_instances == 1
    assert job.coalesce is True
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval == timedelta(hours=1)
    # El scheduler nunca se inicia (.start()): no se abre ningún hilo de
    # background ni se espera tiempo real, solo se inspecciona el job.
    assert scheduler.running is False
