"""Live meeting translator: English speech -> Thai captions + Thai voice.

Run:  python main.py
"""
import os

# numpy is only used for resampling and the level meter. One BLAS thread is
# plenty, and stops OpenBLAS reserving ~300 MB of per-thread buffers.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

from translator.ui import run  # noqa: E402

if __name__ == "__main__":
    run()
