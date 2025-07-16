# gunicorn.conf.py
import os

workers = 1
worker_class = "sync"
worker_tmp_dir = "/dev/shm"
timeout = 120
bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"
