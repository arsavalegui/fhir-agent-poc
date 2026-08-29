"""Vigila una carpeta local (buzón). Cuando cae un archivo FHIR .json nuevo, lo
lee y lo guarda automáticamente en Postgres, luego lo mueve a procesados/.
Es el patrón "landing folder" de un lakehouse, pero local y sin pipeline."""
import os
import time
import shutil
import traceback

import psycopg2
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler

import ingesta

BASE = os.environ.get("BUZON_DIR", "/datos_fhir")
ENTRADA = os.path.join(BASE, "entrada")
PROCESADOS = os.path.join(BASE, "procesados")
ERRORES = os.path.join(BASE, "errores")
DSN = os.environ["DATABASE_URL"]

for d in (ENTRADA, PROCESADOS, ERRORES):
    os.makedirs(d, exist_ok=True)


def esperar_estable(ruta, intentos=10):
    """Espera a que el archivo termine de escribirse (tamaño estable)."""
    ant = -1
    for _ in range(intentos):
        try:
            act = os.path.getsize(ruta)
        except OSError:
            return False
        if act == ant and act > 0:
            return True
        ant = act
        time.sleep(0.6)
    return True


def procesar(ruta):
    nombre = os.path.basename(ruta)
    if not nombre.lower().endswith(".json"):
        return
    if not esperar_estable(ruta):
        return
    print(f"[watcher] nuevo archivo: {nombre}", flush=True)
    try:
        conn = psycopg2.connect(DSN)
        cur = conn.cursor()
        n = ingesta.cargar_bundle(cur, ruta)
        conn.commit()
        cur.close()
        conn.close()
        destino = os.path.join(PROCESADOS, nombre)
        shutil.move(ruta, destino)
        print(f"[watcher] OK: {n} recursos cargados → procesados/{nombre}", flush=True)
    except Exception:
        print(f"[watcher] ERROR con {nombre}:\n{traceback.format_exc()}", flush=True)
        try:
            shutil.move(ruta, os.path.join(ERRORES, nombre))
        except OSError:
            pass


class Manejador(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            procesar(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            procesar(event.dest_path)


if __name__ == "__main__":
    # Procesar lo que ya esté en el buzón al arrancar.
    for f in sorted(os.listdir(ENTRADA)):
        procesar(os.path.join(ENTRADA, f))
    # PollingObserver funciona bien con carpetas montadas en Docker.
    obs = PollingObserver()
    obs.schedule(Manejador(), ENTRADA, recursive=False)
    obs.start()
    print(f"[watcher] vigilando {ENTRADA} … deja archivos FHIR .json ahí.", flush=True)
    try:
        while True:
            time.sleep(2)
    except KeyboardInterrupt:
        obs.stop()
    obs.join()
