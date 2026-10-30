"""Entry point of the network-less `sandbox` container."""
from yonexis.sandbox.runner import worker_loop

if __name__ == "__main__":
    worker_loop()
