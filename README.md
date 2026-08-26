# ObvioBot

Bot de trading en Polymarket que compra tokens "No" en mercados donde el
resultado se considera casi imposible (cotizando entre 94-99 centavos),
principalmente mercados de temperatura de ciudades con umbral simple
("¿la maxima sera X°C?").

## Archivos principales

- `obviobot.py` — logica principal del bot (escaneo, filtros, estrategia)
- `executor.py` — ejecucion de ordenes contra el CLOB de Polymarket
- `security_monitor.py` — monitor que alerta por Telegram ante caidas de
  balance sospechosas
- `serve_dashboard.py` / `obviobot_dashboard.html` — dashboard de estado
- `requirements.txt` — dependencias de Python

## Setup

1. Clonar el repo y crear un entorno virtual:
```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
```

2. **Crear el archivo `.env`** a partir de la plantilla incluida — este
   archivo NO viene en el repositorio por seguridad (contiene la clave
   privada de la wallet y las credenciales de API):
```bash
   cp .env.example .env
   nano .env
```
   Completar todas las variables (`POLY_PRIVATE_KEY`, `POLY_FUNDER`,
   `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE`,
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, etc.). Ver `.env.example`
   para la lista completa.

   **La clave privada nunca se comparte por chat, email ni ningun otro
   medio** — se escribe directamente en el `.env` del servidor donde va a
   correr el bot.

3. Correr el bot:
```bash
   python3 obviobot.py --live
```
   (o sin `--live` para modo paper/simulacion).

## Seguridad

- El `.env` esta en `.gitignore` — jamas debe commitearse.
- Si en algun momento una clave privada o credencial se expuso por error
  (chat, commit, log), se considera comprometida: hay que rotarla /
  generar una wallet nueva, no basta con borrarla del historial.
