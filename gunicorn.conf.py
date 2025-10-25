# gunicorn.conf.py
import multiprocessing

# Worker Class (optional, consider if sync workers are too slow)
# worker_class = 'gevent' # Requires installing gevent (pip install gevent)

# Number of workers
workers = 20  # Keep low for Render stability

# Bind address and port
bind = "0.0.0.0:10000" # Render requires 0.0.0.0 and provides PORT

# Timeout
timeout = 180  # Worker timeout in seconds

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"

# Keep alive connections
keepalive = 35