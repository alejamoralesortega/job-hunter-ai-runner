# Job Hunter AI -- runner

Este repo corre el ciclo de búsqueda + postulación automática de [Job Hunter AI](https://job-hunter-ai-theta.vercel.app)
para **un solo usuario**, en su propia cuenta de GitHub. Se genera automáticamente al conectar
GitHub desde la sección de Ajustes del dashboard -- no hace falta tocar nada acá manualmente.

## Cómo funciona

- El workflow (`.github/workflows/job-search.yml`) corre cada 4 horas vía GitHub Actions
  (gratis e ilimitado por ser un repo público) y llama a `python main.py`.
- `main.py` NUNCA se conecta directo a Supabase ni tiene el token del bot de Telegram. En vez de
  eso, llama a la API del dashboard (`remote_sync.py`) autenticado con `DASHBOARD_API_TOKEN` --
  un secreto propio de este repo que solo puede leer/escribir los datos de este usuario.
- Trae credenciales de plataforma y el CV ya descifrados/extraídos desde
  `GET /api/cron/context`, y reporta cada oferta procesada a `POST /api/cron/report`.
- Los únicos secretos configurados en este repo son `DASHBOARD_API_TOKEN` y `GEMINI_API_KEY`
  (tu propia API key gratuita de Gemini, de [Google AI Studio](https://aistudio.google.com/apikey)).

## Mantenimiento

La lógica de scraping/scoring/auto-apply (`auto_apply.py`, `fetch_jobs.py`, `fetch_scraped.py`,
`score_match.py`, `generate_cv.py`) es una copia de la del repo privado del cron central --
**no un paquete compartido**. Si se arregla un bug ahí (ej. un selector de auto-apply que
cambió), hay que replicarlo a mano acá. Con un puñado de usuarios no vale la pena automatizar
esto (ej. con un paquete pip propio); si el número de repos conectados crece mucho, seria el
primer punto a revisar.
