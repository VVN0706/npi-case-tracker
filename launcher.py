import os
import sys
import threading
import time
import webbrowser

# In the packaged exe the database must live next to the exe, not inside its temp bundle.
if getattr(sys, 'frozen', False):
    os.environ.setdefault('NPI_CASE_DATA_DIR', os.path.dirname(sys.executable))

from app import app

PORT = int(os.environ.get('NPI_CASE_PORT', 8090))


def open_browser():
    time.sleep(1.5)
    webbrowser.open(f'http://127.0.0.1:{PORT}')


if __name__ == '__main__':
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False, threaded=True)
