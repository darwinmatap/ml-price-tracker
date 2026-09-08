"""
Configuración global de logging de la aplicación.

Sin esto, el logger raíz de Python no tiene ningún handler propio: ante
la ausencia de configuración, Python instala su "handler de último
recurso" (logging.lastResort) recién en el momento de emitir un mensaje,
que solo deja pasar WARNING y superior, sin timestamp ni nombre del
logger. Eso silenciaba en producción cualquier logger.info(...) usado
para diagnóstico (ver el log temporal en app/ml_client.py) — se detectó
tras dos intentos fallidos de leer logs de Render que simplemente nunca
se emitieron.

configure_logging() debe llamarse una única vez, al arrancar la app (ver
app/main.py), antes de que cualquier otro módulo loguee algo — así el
handler que instala queda disponible desde el primer mensaje real.
"""

import logging


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
