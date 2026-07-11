#!/usr/bin/env python3
"""Railway web service wrapper - keeps the service alive and runs scraper periodically."""
import subprocess, threading, time, os, logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("app")

def run_scraper_loop():
    """Run main.py every 8 hours."""
    while True:
        try:
            logger.info("Starting scraper run...")
            subprocess.run(["python", "main.py"], check=False)
            logger.info("Scraper run finished, sleeping 8 hours...")
        except Exception as e:
            logger.error(f"Scraper error: {e}")
        time.sleep(8 * 3600)

# Start scraper in background thread
threading.Thread(target=run_scraper_loop, daemon=True).start()

# Minimal HTTP server for Railway health check
from http.server import HTTPServer, BaseHTTPRequestHandler

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, *args):
        pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"Health server listening on port {port}")
    server.serve_forever()
