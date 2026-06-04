import pathlib
import sys

# Rende importabile il package `mail_sender` quando i test girano dal venv
# principale del repo (senza installare il service).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
