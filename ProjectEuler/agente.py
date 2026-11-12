import argparse
import html
import re
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
import time
import random

from google import genai
from google.genai import types

BASE_DIR = Path(__file__).resolve().parent
PROBLEMAS_DIR = BASE_DIR / "problemas"
SOLUCIONES_DIR = BASE_DIR / "soluciones"
LOG_FILE = BASE_DIR / "agent_log.jsonl"

MAX_INTENTOS = 5
TIMEOUT_EJECUCION = 60
MODELO_GEMINI = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

SYSTEM_PROMPT = f"""
Sos un experto en Python y matemática computacional.
Resolvé el problema de Project Euler que te envíe el usuario.
Respondé con un único bloque ```python ... ```.
Debe ser un programa completo y autónomo, usando solamente la biblioteca
estándar de Python, sin pedir datos por teclado ni usar archivos externos.
La última línea de salida debe ser únicamente la respuesta final como entero.
Elegí un algoritmo que termine en menos de {TIMEOUT_EJECUCION} segundos.
"""

def generar_con_reintentos(funcion, max_reintentos=5):
    for intento in range(max_reintentos):
        try:
            return funcion()

        except Exception as error:
            mensaje = str(error).lower()

            # Identificar errores temporales de Gemini
            temporal = any(palabra in mensaje for palabra in [
                "high demand",
                "503",
                "429",
                "resource_exhausted",
                "unavailable"
            ])

            if not temporal or intento == max_reintentos - 1:
                raise

            espera = min(5 * (2 ** intento), 60)

            print(
                f"Gemini está saturado. "
                f"Reintentando en {espera} segundos..."
            )

            time.sleep(espera)

def html_a_texto(contenido):
    contenido = re.sub(
        r"<(script|style).*?</\\1>", "", contenido,
        flags=re.IGNORECASE | re.DOTALL
    )
    contenido = re.sub(r"<br\\s*/?>|</p>|</div>|</li>", "\n", contenido)
    contenido = re.sub(r"<sup>(.*?)</sup>", r"^\\1", contenido)
    contenido = re.sub(r"<sub>(.*?)</sub>", r"_\\1", contenido)
    contenido = re.sub(r"<[^>]+>", "", contenido)
    return html.unescape(contenido).strip()


def obtener_enunciado(numero):
    PROBLEMAS_DIR.mkdir(parents=True, exist_ok=True)
    archivo = PROBLEMAS_DIR / f"problema_{numero:03d}.txt"

    if archivo.exists():
        return archivo.read_text(encoding="utf-8")

    urls = [
        f"https://projecteuler.net/minimal={numero}",
        f"https://projecteuler.net/problem={numero}",
    ]
    headers = {"User-Agent": "Mozilla/5.0 ProjectEulerStudentAgent/1.0"}

    for url in urls:
        try:
            solicitud = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(solicitud, timeout=25) as respuesta:
                pagina = respuesta.read().decode("utf-8", errors="replace")

            if 'class="problem_content"' in pagina:
                pagina = pagina.split('class="problem_content"', 1)[1]
                pagina = pagina.split('id="footer"', 1)[0]

            texto = html_a_texto(pagina)
            if len(texto) > 30:
                archivo.write_text(texto, encoding="utf-8")
                print(f"Enunciado guardado: {archivo}")
                return texto
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            print(f"No se pudo descargar desde {url}: {error}")

    raise RuntimeError(
        f"No se pudo obtener el problema {numero}. Copiá el enunciado desde "
        f"https://projecteuler.net/problem={numero} y guardalo en {archivo}."
    )


def llamar_gemini(cliente, historial):
    def solicitar():
        respuesta = cliente.models.generate_content(
            model=MODELO_GEMINI,
            contents=historial,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.2,
            ),
        )
        return respuesta.text or ""

    return generar_con_reintentos(solicitar)


def extraer_codigo(respuesta):
    # Busca un bloque de código Python
    patron = r"```(?:python)?\s*(.*?)```"

    coincidencia = re.search(
        patron,
        respuesta,
        re.IGNORECASE | re.DOTALL
    )

    if coincidencia:
        return coincidencia.group(1).strip()

    # Si Gemini no utiliza bloques Markdown,
    # devuelve la respuesta directamente.
    return respuesta.strip()


def ejecutar_codigo(ruta):
    entorno = os.environ.copy()
    entorno["PYTHONIOENCODING"] = "utf-8"
    inicio = time.perf_counter()

    try:
        proceso = subprocess.run(
            [sys.executable, str(ruta)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_EJECUCION,
            cwd=BASE_DIR,
            env=entorno,
        )
        duracion = time.perf_counter() - inicio
        return (
            proceso.returncode, proceso.stdout.strip(),
            proceso.stderr.strip(), duracion, False
        )
    except subprocess.TimeoutExpired as error:
        duracion = time.perf_counter() - inicio
        salida = error.stdout or ""
        if isinstance(salida, bytes):
            salida = salida.decode("utf-8", errors="replace")
        return -1, salida.strip(), "Se superó el tiempo máximo.", duracion, True


def extraer_respuesta(salida):
    if not salida:
        return None
    ultima = salida.splitlines()[-1].strip()
    return ultima if re.fullmatch(r"-?\\d+", ultima) else None


def registrar_intento(numero, intento, codigo, salida, error,
                      duracion, resultado, respuesta):
    registro = {
        "fecha": datetime.now().isoformat(timespec="seconds"),
        "problema": numero,
        "intento": intento,
        "codigo": codigo,
        "salida": salida,
        "error": error[-3000:],
        "tiempo_segundos": round(duracion, 3),
        "resultado": resultado,
        "respuesta": respuesta,
    }
    with LOG_FILE.open("a", encoding="utf-8") as archivo:
        archivo.write(json.dumps(registro, ensure_ascii=False) + "\n")


def resolver_problema(cliente, numero):
    print(f"\n{'=' * 55}\nPROBLEMA {numero}\n{'=' * 55}")

    try:
        enunciado = obtener_enunciado(numero)
    except RuntimeError as error:
        print(error)
        return False, 0

    SOLUCIONES_DIR.mkdir(parents=True, exist_ok=True)
    temporal = SOLUCIONES_DIR / f"_intento_{numero:03d}.py"

    historial = [
        types.Content(
            role="user",
            parts=[types.Part(text=(
                f"Resolvé el problema {numero} de Project Euler.\n\n"
                f"ENUNCIADO:\n{enunciado}\n\nGenerá el programa Python."
            ))],
        )
    ]
    respuestas_rechazadas = set()

    for intento in range(1, MAX_INTENTOS + 1):
        print(f"\nIntento {intento}/{MAX_INTENTOS}: consultando a Gemini...")

        try:
            texto_modelo = llamar_gemini(cliente, historial)
        except Exception as error:
            print(f"Error al consultar Gemini: {error}")
            registrar_intento(
                numero, intento, "", "", str(error), 0, "error_gemini", None
            )
            break

        codigo = extraer_codigo(texto_modelo)
        salida = ""
        error_ejecucion = ""
        duracion = 0
        respuesta = None

        if not codigo:
            resultado = "sin_codigo"
            feedback = "No devolviste código Python. Enviá un bloque ```python```."
        else:
            temporal.write_text(codigo + "\n", encoding="utf-8")
            rc, salida, error_ejecucion, duracion, timeout = ejecutar_codigo(temporal)
            respuesta = extraer_respuesta(salida)

            if timeout:
                resultado = "timeout"
                feedback = (
                    f"El programa superó {TIMEOUT_EJECUCION} segundos. "
                    "Optimizá el algoritmo."
                )
            elif rc != 0:
                resultado = "error_ejecucion"
                feedback = (
                    f"El programa terminó con código {rc}.\n"
                    f"Error:\n{error_ejecucion}\nSalida:\n{salida}"
                )
            elif respuesta is None:
                resultado = "sin_respuesta_numerica"
                feedback = (
                    "La última línea no contiene un número entero. "
                    f"Salida obtenida:\n{salida}"
                )
            elif respuesta in respuestas_rechazadas:
                resultado = "respuesta_repetida"
                feedback = (
                    f"La respuesta {respuesta} ya fue rechazada. "
                    "Revisá el enfoque y generá otra solución."
                )
            else:
                print(f"\nRespuesta calculada: {respuesta}")
                print(f"Tiempo: {duracion:.3f} segundos")
                while True:
                    confirmacion = input(
                        "¿Project Euler aceptó la respuesta? [s/n/q]: "
                    ).strip().lower()
                    if confirmacion in ("s", "n", "q"):
                        break
                    print("Ingresá s, n o q.")

                if confirmacion == "s":
                    registrar_intento(
                        numero, intento, codigo, salida, error_ejecucion,
                        duracion, "correcta", respuesta
                    )
                    final = SOLUCIONES_DIR / f"problema_{numero:03d}.py"
                    final.write_text(codigo + "\n", encoding="utf-8")
                    temporal.unlink(missing_ok=True)
                    print(f"¡Resuelto! Solución guardada en {final}")
                    return True, intento

                if confirmacion == "q":
                    registrar_intento(
                        numero, intento, codigo, salida, error_ejecucion,
                        duracion, "abandonado", respuesta
                    )
                    temporal.unlink(missing_ok=True)
                    return False, intento

                resultado = "respuesta_incorrecta"
                respuestas_rechazadas.add(respuesta)
                feedback = (
                    f"Project Euler rechazó {respuesta}. Revisá el enunciado, "
                    "los límites y los casos especiales. Corregí el programa."
                )

        registrar_intento(
            numero, intento, codigo, salida, error_ejecucion,
            duracion, resultado, respuesta
        )
        print(f"Resultado del intento: {resultado}")
        if error_ejecucion:
            print(error_ejecucion[:700])

        historial.append(
            types.Content(role="model", parts=[types.Part(text=texto_modelo)])
        )
        historial.append(
            types.Content(role="user", parts=[types.Part(text=feedback)])
        )

    temporal.unlink(missing_ok=True)
    print(f"No se pudo resolver el problema {numero}.")
    return False, MAX_INTENTOS


def main():
    parser = argparse.ArgumentParser(
        description="Agente de IA para Project Euler (problemas 9 al 20)."
    )
    parser.add_argument("--desde", type=int, default=9)
    parser.add_argument("--hasta", type=int, default=20)
    args = parser.parse_args()

    if not (9 <= args.desde <= 20 and 9 <= args.hasta <= 20):
        parser.error("Los problemas deben estar entre el 9 y el 20.")
    if args.desde > args.hasta:
        parser.error("--desde no puede ser mayor que --hasta.")

    if not os.environ.get("GEMINI_API_KEY"):
        print('Configurá tu clave en PowerShell: $env:GEMINI_API_KEY="TU_CLAVE"')
        return

    cliente = genai.Client()
    resumen = []

    for numero in range(args.desde, args.hasta + 1):
        resuelto, intentos = resolver_problema(cliente, numero)
        resumen.append((numero, resuelto, intentos))

    print("\nRESUMEN")
    for numero, resuelto, intentos in resumen:
        estado = "RESUELTO" if resuelto else "NO RESUELTO"
        print(f"Problema {numero}: {estado} ({intentos} intento(s))")
    print(f"\nLog guardado en: {LOG_FILE}")


if __name__ == "__main__":
    main()
