# SmartVyapar Backend - Hostinger Node.js Deployment Guide

## Hostinger Node.js Hosting par Setup karne ka Tareeqa:

1. Hostinger me **Node.js App** create karein:
   - **Node.js Version:** `18.x`, `20.x`, ya `22.x` select karein.
   - **Application Root:** Apne project folder ka path (e.g., `public_html/api` ya `public_html`).
   - **Application Startup File:** `server.js`

2. Python Dependencies Install karein:
   - Hostinger SSH / Web Terminal open karein aur project folder me ja kar yeh command chalayein:
     `pip install -r requirements.txt` (ya `pip3 install -r requirements.txt`)

3. Node.js App Start karein:
   - Hostinger Node.js manager me **Start** ya **Restart** par click karein.
   - `server.js` automatically Python FastAPI ko start karega aur saari API requests forward karega!
