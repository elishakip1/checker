# gunicorn.conf.py
import multiprocessing

# Worker Class (optional, but good for performance)
# worker_class = 'sync' # Default, simple
# worker_class = 'gevent' # Requires installing gevent (pip install gevent)

# Number of workers
# workers = multiprocessing.cpu_count() * 2 + 1 # Common formula, but too high for Render Free/Pro
workers = 2  # Keep this low (2 or 3) for Render stability

# Bind address and port
bind = "0.0.0.0:10000" # Render requires binding to 0.0.0.0 and provides the PORT env var

# Timeout
timeout = 180  # Increase worker timeout to 180 seconds (3 minutes)

# Logging
accesslog = "-"  # Log access requests to stdout
errorlog = "-"   # Log errors to stdout
loglevel = "info" # Gunicorn log level

# Keep alive connections
keepalive = 5
