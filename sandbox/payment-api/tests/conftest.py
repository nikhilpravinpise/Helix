import os

# Required config must be present before payment_api.main is imported (it
# builds Settings at module import time and fails fast otherwise).
os.environ.setdefault("PAYMENT_API_API_KEY", "test-key")
os.environ.setdefault("PAYMENT_API_DOWNSTREAM_URL", "http://downstream.example/pay")
