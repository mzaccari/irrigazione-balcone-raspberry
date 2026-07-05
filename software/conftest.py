"""Rende importabili i moduli in software/ quando si esegue pytest dalla radice.

Cosi i test (software/tests/*.py) possono fare `import scheduler`, `import store`,
ecc. senza dipendere dalla cartella da cui viene lanciato pytest.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
